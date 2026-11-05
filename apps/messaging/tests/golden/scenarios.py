"""Golden scenarios: fixed sample orders whose automated messages are recorded word for word.

Each scenario builds its order on a frozen clock, plays the clock forward to the moment its
message is due (sending everything due earlier on the way, as the cron would), and returns
the TouchPoint under test. `test_golden.capture` then runs the `run-touchpoints` cron job
once at that moment and records every send.

Everything the messaging engine's own API is called for lives in the small helpers under
"Engine seams" below; the rest goes through domain services (booking, payments, dispatch,
trip status). A port of the engine edits those helpers and nothing else.
"""

import json
import re
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import stripe
from django.core import signing

from apps.accounts.factories import UserFactory
from apps.billing.factories import AccountGroupFactory, BillingAccountFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.payments import services as payments
from apps.payments.models import Charge, PaymentPlan
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.reservations.services import (
    confirm_trip_day,
    set_trip_status,
    trip_day_group,
    wedding_day_group,
)
from apps.vendors.factories import VendorFactory

K = TouchPoint.Kind
BASE_URL = "https://example.test"
EASTERN = ZoneInfo("America/New_York")
PACIFIC = ZoneInfo("America/Los_Angeles")

# The day every scenario starts on: Monday Mar 1, 2027, 10:00 AM Eastern.
SETUP = datetime(2027, 3, 1, 15, 0, tzinfo=UTC)
# Pickup days. Every send lands between 8 AM and 9 PM in the trip's own zone, so a later
# quiet-hours rule has nothing to hold.
PLAIN_DAY = date(2027, 4, 20)
WEDDING_DAY = date(2027, 4, 24)
BALANCE_DAY = date(2027, 5, 14)  # far enough out that the balance is due after SETUP

DANA = {"name": "Dana Whitfield", "email": "dana.whitfield@example.com", "phone": "+12025550101"}
CASEY = {"name": "Casey Morgan", "email": "casey.morgan@example.com", "phone": "+12025550102"}
PAT = {"name": "Pat Okafor", "email": "pat.okafor@example.com", "phone": "+12025550103"}
VENDOR = {
    "name": "Capital Coach Lines",
    "contact_name": "Morgan Lee",
    "email": "dispatch@capitalcoach.example.com",
    "phone": "+12025550188",
}
DRIVER = {
    "name": "Sam Rivera",
    "cell": "+12025550199",
    "vehicle_desc": "Black Cadillac Escalade",
    "vehicle_number": "12",
}
NEW_DRIVER_CELL = "+12025550177"

DC_ROUTE = (
    "1600 Pennsylvania Ave NW, Washington, DC 20500",
    "Washington Dulles International Airport (IAD)",
)
LA_ROUTE = ("200 N Spring St, Los Angeles, CA 90012", "Los Angeles International Airport (LAX)")
CEREMONY_ROUTE = ("The Hay-Adams, 800 16th St NW, Washington, DC", "St. Matthew's Cathedral")
RECEPTION_ROUTE = ("St. Matthew's Cathedral", "Anderson House, 2118 Massachusetts Ave NW")


# --- clock ---------------------------------------------------------------------------------


class Clock:
    """The one clock every `timezone.now()` reads while a scenario runs."""

    def __init__(self) -> None:
        self.now = SETUP

    def set(self, moment: datetime) -> None:
        self.now = moment


clock = Clock()


@contextmanager
def frozen() -> Iterator[Clock]:
    """Freeze `django.utils.timezone.now` on `clock`, starting at SETUP."""
    clock.set(SETUP)
    with patch("django.utils.timezone.now", side_effect=lambda: clock.now):
        yield clock


def local(day: date, at: time, zone: ZoneInfo = EASTERN) -> datetime:
    return datetime.combine(day, at, tzinfo=zone)


def run_cron() -> int:
    """The stable entry point: the `run-touchpoints` job, looked up by name."""
    from apps.core.cron import JOBS

    return JOBS["run-touchpoints"]()


def advance(moment: datetime) -> None:
    """Move the clock to `moment`, running the cron at each earlier send time on the way,
    so whatever was due before then has gone out (or stood down) as it would have."""
    last = None
    while True:
        due = TouchPoint.objects.filter(
            status=TouchPoint.Status.SCHEDULED, scheduled_for__lt=moment
        )
        if last is not None:
            due = due.filter(scheduled_for__gt=last)
        at = due.order_by("scheduled_for").values_list("scheduled_for", flat=True).first()
        if at is None:
            break
        clock.set(max(at, clock.now))
        run_cron()
        last = at
    clock.set(moment)


def ready(tp: TouchPoint, *, run_at: datetime | None = None) -> TouchPoint:
    """Play the clock up to `tp`'s send time and leave it there (or at `run_at`, later the
    same morning, for a row due in the small hours)."""
    advance(tp.scheduled_for)
    if run_at is not None:
        clock.set(run_at)
    return tp


# --- normalising -------------------------------------------------------------------------

_LINK = re.compile(re.escape(BASE_URL) + r"/\S+")
_QUOTE_NO = re.compile(r"\b(?:Q|APC)-\d+\b")


def _trip_label(trip: Reservation) -> str:
    return f"{trip.lead.contact.name} {trip.pickup_date} {trip.pickup_time:%H:%M}"


def _lead_label(lead: Lead) -> str:
    first = lead.reservations.order_by("pickup_date", "pickup_time", "pk").first()
    return _trip_label(first) if first else lead.contact.name


def _assignment_label(assignment: Assignment) -> str:
    return assignment.vendor.name if assignment.vendor_id else assignment.driver.name


# What each id in a signed link payload points at, by a name that is the same every run.
_TARGETS = {
    "contact": lambda pk: Contact.objects.get(pk=pk).name,
    "lead": lambda pk: _lead_label(Lead.objects.get(pk=pk)),
    "reservation": lambda pk: _trip_label(Reservation.objects.get(pk=pk)),
    "assignment": lambda pk: _assignment_label(Assignment.objects.get(pk=pk)),
}


def _token(segment: str) -> str:
    """A signed link token (`payload:timestamp:signature`, payload `.`-prefixed when
    compressed) rendered as what it points at, e.g. `<TOKEN contact=Dana Whitfield
    date=2027-04-20>`, so a link signed for the wrong person, trip or day fails."""
    head = segment.split(":", 1)[0]
    raw = signing.b64_decode(head.lstrip(".").encode())
    if head.startswith("."):
        raw = zlib.decompress(raw)
    payload = json.loads(raw)
    parts = [
        f"{key}={_TARGETS[key](value) if key in _TARGETS else value}"
        for key, value in sorted(payload.items())
    ]
    return f"<TOKEN {' '.join(parts)}>"


def _link(match: re.Match) -> str:
    """Keep a link's page; render its signed token by target, any bare pk as <PK>."""
    segments = match.group(0)[len(BASE_URL) :].split("/")
    return BASE_URL + "/".join(
        _token(s) if ":" in s else "<PK>" if s.isdigit() else s for s in segments
    )


def _number_quotes(text: str) -> str:
    """Quote numbers become <Q1>, <Q2>, ... in order of first appearance in this text."""
    seen: dict[str, str] = {}
    return _QUOTE_NO.sub(lambda m: seen.setdefault(m.group(0), f"<Q{len(seen) + 1}>"), text)


def normalise(text: str) -> str:
    """Strip what differs between runs (token signatures, pks, quote numbers), nothing else.
    Reads the database to name a token's targets, so call it before the test rolls back."""
    return _number_quotes(_LINK.sub(_link, text))


# --- engine seams (the only calls into the messaging engine's own API) ---------------------


def _lead_came_in(lead: Lead) -> None:
    """What the New-lead form and the inbox's Create lead do."""
    touchpoints.schedule_lead_created(lead)


def _quote_viewed(lead: Lead) -> None:
    """What the public quote page does on the customer's first view."""
    lead.quote_viewed_at = lead.quote_last_viewed_at = clock.now
    lead.quote_view_count = 1
    lead.save(update_fields=["quote_viewed_at", "quote_last_viewed_at", "quote_view_count"])
    touchpoints.schedule_quote_viewed(lead)


def _trips_done(lead: Lead) -> None:
    """Every trip ran: Done through the status seam, then the order's post-trip messages
    (what the task engine and the LimoAnywhere webhook both do; idempotent)."""
    for trip in lead.reservations.all():
        set_trip_status(trip, Reservation.TripStatus.DONE)
    touchpoints.schedule_post_trip(lead)


def _switch_on(kind: str) -> None:
    """The owner turns on a message that ships switched off."""
    MessageTemplate.objects.update_or_create(kind=kind, defaults={"enabled": True})


def _queue_status_dispatched(trip: Reservation) -> TouchPoint:
    """No code path queues this kind today (it is in the editor's catalog only); queue it
    the way `notify_status_change` queues its siblings, on the Dispatched transition."""
    return TouchPoint.objects.create(
        lead=trip.lead, reservation=trip, kind=K.STATUS_DISPATCHED, scheduled_for=clock.now
    )


def _message(kind: str, *, lead: Lead | None = None, trip: Reservation | None = None):
    """The one live (not cancelled) row of `kind` for a lead or a trip."""
    rows = TouchPoint.objects.filter(kind=kind).exclude(status=TouchPoint.Status.CANCELLED)
    rows = rows.filter(reservation=trip) if trip is not None else rows.filter(lead=lead)
    return rows.get()


# --- building orders -------------------------------------------------------------------


def _agent():
    return UserFactory(
        username="jordan.ellis",
        first_name="Jordan",
        last_name="Ellis",
        email="jordan.ellis@allprocharter.com",
    )


def _company(name: str, *, billing: dict | None = None, operator: bool = False):
    """A customer's company; `operator` marks an affiliate that farms trips in (APC-92)."""
    return CompanyFactory(
        name=name,
        billing_contact=ContactFactory(**billing) if billing else None,
        is_affiliate_account=operator,
    )


def _order(person: dict, *, billing: dict | None = None, company=None) -> Lead:
    return LeadFactory(
        contact=ContactFactory(**person, company=company),
        billing_contact=ContactFactory(**billing) if billing else None,
        assigned_agent=_agent(),
        status=Lead.Status.NEW,
    )


def _force(lead: Lead, **fields) -> None:
    """Put the order in a state without the service that would also tidy its queued
    messages, so the send-time guard is what has to catch it."""
    Lead.objects.filter(pk=lead.pk).update(**fields)


def _trip(
    lead: Lead,
    day: date,
    at: time,
    *,
    zone: ZoneInfo = EASTERN,
    route: tuple[str, ...] = DC_ROUTE,
    wedding: bool = False,
    leg: str = "",
) -> Reservation:
    """`wedding` sets the Wedding service type; `leg` is a legacy wedding-builder leg id
    (`source_leg_id`), a wedding with no Wedding service type."""
    return ReservationFactory(
        source_leg_id=leg,
        lead=lead,
        pickup_date=day,
        pickup_time=at,
        pickup_timezone=str(zone),
        passengers=3,
        rate=Decimal("185.00"),
        hours=1,
        min_hours=0,
        vehicle=VehicleTypeFactory(name="Stretch Limousine" if wedding else "Luxury SUV"),
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None,
        stops=list(route),
    )


@contextmanager
def _no_limoanywhere() -> Iterator[None]:
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _book(lead: Lead) -> Lead:
    with _no_limoanywhere():
        lead_services.book_lead(lead)
    lead.refresh_from_db()
    return lead


def _plain_booked(
    person: dict = DANA, *, at: time = time(14, 0), zone=EASTERN, route=DC_ROUTE, company=None
):
    lead = _order(person, company=company)
    trip = _trip(lead, PLAIN_DAY, at, zone=zone, route=route)
    return _book(lead), trip


def _wedding_booked(
    *,
    legs: int = 2,
    person: dict = CASEY,
    billing: dict | None = PAT,
    company=None,
    legacy: bool = False,
) -> tuple[Lead, list[Reservation]]:
    """By default Casey books and Pat pays: the wedding's confirmations go to Pat (APC-94).
    `legacy` builds the legs the retired wedding builder made (leg ids, no service type)."""
    lead = _order(person, billing=billing, company=company)
    plan = [(time(14, 0), CEREMONY_ROUTE, "ceremony"), (time(18, 0), RECEPTION_ROUTE, "reception")]
    trips = [
        _trip(
            lead,
            WEDDING_DAY,
            at,
            route=route,
            wedding=not legacy,
            leg=leg if legacy else "",
        )
        for at, route, leg in plan[:legs]
    ]
    return _book(lead), trips


def _cover(trip: Reservation) -> Assignment:
    """An affiliate is offered the trip and accepts."""
    offer = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(**VENDOR),
        status=Assignment.Status.OFFERED,
        payout=Decimal("140.00"),
    )
    return dispatch_services.confirm(offer)


def _driver(assignment: Assignment, *, cell: str = DRIVER["cell"]) -> None:
    dispatch_services.set_driver_info(
        assignment,
        name=DRIVER["name"],
        cell=cell,
        vehicle_desc=DRIVER["vehicle_desc"],
        vehicle_number=DRIVER["vehicle_number"],
    )


def _dispatched(*, zone=EASTERN, route=DC_ROUTE) -> tuple[Lead, Reservation, Assignment]:
    """An 8:30 PM pickup, covered with a driver, Dispatched the day before (noon local)."""
    lead, trip = _plain_booked(at=time(20, 30), zone=zone, route=route)
    assignment = _cover(trip)
    _driver(assignment)
    advance(local(PLAIN_DAY - timedelta(days=1), time(12, 0), zone))
    trip.refresh_from_db()
    set_trip_status(trip, Reservation.TripStatus.DISPATCHED)
    return lead, trip, assignment


def _quoted(person: dict = DANA, *, zone: ZoneInfo = EASTERN, route=DC_ROUTE) -> Lead:
    """Quote sent through the Send quote button at SETUP."""
    lead = _order(person)
    _trip(lead, PLAIN_DAY, time(14, 0), zone=zone, route=route)
    result = lead_services.send_quote(lead, base_url=BASE_URL)
    assert result.ok, result.error
    lead.refresh_from_db()
    return lead


def _intent(pi: str, amount: Decimal, status: str) -> MagicMock:
    card = MagicMock(brand="visa", last4="4242", exp_month=8, exp_year=2029)
    return MagicMock(
        id=pi,
        status=status,
        amount=int(amount * 100),
        metadata={},
        payment_method=MagicMock(id="pm_golden", card=card),
    )


def _engaged(person: dict = DANA) -> tuple[Lead, Charge]:
    """The customer authorized the deposit and waits on us (APC-26)."""
    lead = _order(person)
    lead.status = Lead.Status.ENGAGED
    lead.quote_sent_at = clock.now
    lead.save(update_fields=["status", "quote_sent_at"])
    _trip(lead, PLAIN_DAY, time(14, 0))
    lead.quote_expires_at = lead_services.compute_quote_expiry(lead)
    lead.save(update_fields=["quote_expires_at"])
    plan = payments.ensure_plan(lead)
    plan.deposit_status = PaymentPlan.DepositStatus.AUTHORIZED
    plan.save(update_fields=["deposit_status"])
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = "pi_golden"
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = clock.now
    charge.capture_expires_at = clock.now + timedelta(days=7)
    charge.save()
    return lead, charge


def _deposit_paid_by_card(day: date = BALANCE_DAY) -> Lead:
    """A card deposit lands (pay page / webhook): booked, card stored, balance scheduled."""
    lead = _order(DANA)
    _trip(lead, day, time(14, 0))
    plan = payments.ensure_plan(lead)
    intent = _intent("pi_golden", plan.deposit_amount, "succeeded")
    with (
        patch.object(stripe.PaymentIntent, "retrieve", return_value=intent),
        _no_limoanywhere(),
    ):
        payments.record_payment(plan, "pi_golden", kind=Charge.Kind.DEPOSIT)
    lead.refresh_from_db()
    return lead


def _finished(lead: Lead, last_day: date) -> None:
    """The order's trips ran; marked Done at 5 PM on the last day."""
    advance(local(last_day, time(17, 0)))
    _trips_done(lead)


# --- scenarios: one per kind on a plain order -----------------------------------------


def tp1_welcome() -> TouchPoint:
    lead = _order(DANA)
    _trip(lead, PLAIN_DAY, time(14, 0))
    _lead_came_in(lead)
    return ready(_message(K.TP1_WELCOME, lead=lead))


def tp2_lead_followup() -> TouchPoint:
    lead = _order(DANA)
    _trip(lead, PLAIN_DAY, time(14, 0))
    _lead_came_in(lead)
    return ready(_message(K.TP2_LEAD_FOLLOWUP, lead=lead))


def tp3_quote_sent_sms() -> TouchPoint:
    return ready(_message(K.TP3_QUOTE_SENT_SMS, lead=_quoted()))


def _viewed_an_hour_later(**trip) -> Lead:
    lead = _quoted(**trip)
    advance(SETUP + timedelta(hours=1))
    _quote_viewed(lead)
    return lead


def tp4_viewed_sms() -> TouchPoint:
    return ready(_message(K.TP4_VIEWED_SMS, lead=_viewed_an_hour_later()))


def tp5_viewed_email() -> TouchPoint:
    return ready(_message(K.TP5_VIEWED_EMAIL, lead=_viewed_an_hour_later()))


def tp6_quote_followup() -> TouchPoint:
    return ready(_message(K.TP6_QUOTE_FOLLOWUP, lead=_quoted()))


def tp7_expiring() -> TouchPoint:
    return ready(_message(K.TP7_EXPIRING, lead=_quoted()))


def tp8_expired() -> TouchPoint:
    return ready(_message(K.TP8_EXPIRED, lead=_quoted()))


def order_auth_expired() -> TouchPoint:
    lead, charge = _engaged()
    advance(SETUP + timedelta(days=7))
    payments.expire_authorization(charge)
    return ready(_message(K.ORDER_AUTH_EXPIRED, lead=lead))


def order_confirmed() -> TouchPoint:
    lead, charge = _engaged()
    advance(SETUP + timedelta(hours=3))
    captured = _intent("pi_golden", charge.amount, "succeeded")
    with (
        patch.object(stripe.PaymentIntent, "capture"),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=captured),
        _no_limoanywhere(),
    ):
        payments.confirm_order(lead)
    return ready(_message(K.ORDER_CONFIRMED, lead=lead))


def order_cancelled() -> TouchPoint:
    lead, _ = _engaged()
    advance(SETUP + timedelta(hours=3))
    with patch.object(stripe.PaymentIntent, "cancel"):
        payments.cancel_order(lead, reason="No vehicle available that day")
    return ready(_message(K.ORDER_CANCELLED, lead=lead))


def payment_reminder() -> TouchPoint:
    lead = _deposit_paid_by_card()
    tp = _message(K.PAYMENT_REMINDER, lead=lead)
    # Due at midnight Eastern; the cron that picks it up runs at 10 AM.
    return ready(tp, run_at=tp.scheduled_for + timedelta(hours=10))


def trip_confirm_customer() -> TouchPoint:
    _, trip = _plain_booked()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trip))


def trip_confirm_customer_2() -> TouchPoint:
    _, trip = _plain_booked()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trip))


def trip_confirm_affiliate() -> TouchPoint:
    """The affiliate's 48h confirmation. The customer already confirmed their day, so the
    customer's own 48h notice (due the same minute) stands down."""
    lead, trip = _plain_booked()
    _cover(trip)
    advance(SETUP + timedelta(days=1))
    confirm_trip_day(trip_day_group(lead.contact, PLAIN_DAY))
    return ready(_message(K.TRIP_CONFIRM_AFFILIATE, trip=trip))


def driver_released() -> TouchPoint:
    _, trip, _ = _dispatched()
    return ready(_message(K.DRIVER_RELEASED, trip=trip))


def driver_updated() -> TouchPoint:
    """The driver message went out at 8:30 AM; at 9:30 the driver's cell changes."""
    _, trip, assignment = _dispatched()
    advance(local(PLAIN_DAY, time(9, 30)))
    assignment.refresh_from_db()
    _driver(assignment, cell=NEW_DRIVER_CELL)
    return ready(_message(K.DRIVER_UPDATED, trip=trip))


def status_dispatched() -> TouchPoint:
    _switch_on(K.STATUS_DISPATCHED)
    _, trip, _ = _dispatched()
    return ready(_queue_status_dispatched(trip))


def status_on_the_way() -> TouchPoint:
    _switch_on(K.STATUS_ON_THE_WAY)
    _, trip, _ = _dispatched()
    advance(local(PLAIN_DAY, time(20, 0)))
    trip.refresh_from_db()
    set_trip_status(trip, Reservation.TripStatus.ON_THE_WAY)
    return ready(_message(K.STATUS_ON_THE_WAY, trip=trip))


def status_arrived() -> TouchPoint:
    _switch_on(K.STATUS_ARRIVED)
    _, trip, _ = _dispatched()
    advance(local(PLAIN_DAY, time(20, 25)))
    trip.refresh_from_db()
    set_trip_status(trip, Reservation.TripStatus.ARRIVED)
    return ready(_message(K.STATUS_ARRIVED, trip=trip))


def wed_final_details() -> TouchPoint:
    """The 7-day details request goes to the booker (not the billing contact), per leg."""
    _, trips = _wedding_booked()
    return ready(_message(K.WED_FINAL_DETAILS, trip=trips[0]))


def thank_you() -> TouchPoint:
    lead, _ = _plain_booked()
    _finished(lead, PLAIN_DAY)
    return ready(_message(K.THANK_YOU, lead=lead))


def review_request() -> TouchPoint:
    lead, _ = _plain_booked()
    _finished(lead, PLAIN_DAY)
    return ready(_message(K.REVIEW_REQUEST, lead=lead))


# --- scenarios: the rules that bend who gets what --------------------------------------


def wedding_confirm_72h_to_billing_contact() -> TouchPoint:
    """Casey booked, Pat pays: Pat gets the 72h confirmation for both legs, wedding link."""
    _, trips = _wedding_booked()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trips[0]))


def wedding_confirm_48h_to_billing_contact() -> TouchPoint:
    _, trips = _wedding_booked()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trips[0]))


def wedding_thank_you() -> TouchPoint:
    lead, _ = _wedding_booked()
    _finished(lead, WEDDING_DAY)
    return ready(_message(K.THANK_YOU, lead=lead))


def _two_orders_same_day(
    *, later: time = time(16, 0), zone: ZoneInfo = EASTERN, route=DC_ROUTE
) -> tuple[Reservation, Reservation]:
    """Dana books twice for the same local day: a 10 AM and a later one, separate orders."""
    morning_lead = _order(DANA)
    morning = _trip(morning_lead, PLAIN_DAY, time(10, 0), zone=zone, route=route)
    _book(morning_lead)
    afternoon_lead = LeadFactory(contact=morning_lead.contact, assigned_agent=_agent())
    afternoon = _trip(afternoon_lead, PLAIN_DAY, later, zone=zone, route=tuple(reversed(route)))
    _book(afternoon_lead)
    return morning, afternoon


def two_trips_same_day_one_message() -> TouchPoint:
    morning, _ = _two_orders_same_day()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=morning))


def two_trips_same_day_second_stands_down() -> TouchPoint:
    _, afternoon = _two_orders_same_day()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=afternoon))


def wedding_and_plain_same_day_two_messages() -> TouchPoint:
    """Casey books a wedding (Pat pays) and a plain trip at the same hour on the same day:
    the plain notice goes to Casey, the wedding's to Pat. Neither stands in for the other."""
    _, (wedding_trip,) = _wedding_booked(legs=1)
    plain_lead = LeadFactory(contact=wedding_trip.lead.contact, assigned_agent=_agent())
    plain = _trip(plain_lead, WEDDING_DAY, time(14, 0), route=tuple(reversed(DC_ROUTE)))
    _book(plain_lead)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=plain))


def trip_confirm_customer_2_day_confirmed() -> TouchPoint:
    """The second notice only goes while the day is unconfirmed."""
    lead, trip = _plain_booked()
    advance(SETUP + timedelta(days=1))
    confirm_trip_day(trip_day_group(lead.contact, PLAIN_DAY))
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trip))


def driver_released_pacific_trip() -> TouchPoint:
    """A Los Angeles pickup: times carry the trip's own zone abbreviation."""
    _, trip, _ = _dispatched(zone=PACIFIC, route=LA_ROUTE)
    return ready(_message(K.DRIVER_RELEASED, trip=trip))


def payment_reminder_account_order() -> TouchPoint:
    """The order moved to a billing account after the reminder was queued: skipped when due.
    `bill_to_account` refuses once a card holds money, so the switch is forced directly."""
    lead = _deposit_paid_by_card()
    group = AccountGroupFactory(
        account=BillingAccountFactory(contact=lead.contact, name="Ridgeline Partners"),
        name="Accounts Payable",
        invoice_email="ap@ridgeline.example.com",
    )
    Lead.objects.filter(pk=lead.pk).update(billing_group=group)
    tp = _message(K.PAYMENT_REMINDER, lead=lead)
    return ready(tp, run_at=tp.scheduled_for + timedelta(hours=10))


def review_request_held_by_complaint() -> TouchPoint:
    lead, trip = _plain_booked()
    _finished(lead, PLAIN_DAY)
    advance(local(PLAIN_DAY, time(18, 0)))
    reviews.add_issue(
        reviews.review_for(trip),
        category=TripIssue.Category.COMPLAINT,
        severity=TripIssue.Severity.HIGH,
        note="Driver arrived 40 minutes late.",
        user=lead.assigned_agent,
    )
    return ready(_message(K.REVIEW_REQUEST, lead=lead))


# --- scenarios: fix round 1 (routing gaps) ---------------------------------------------

MORGAN_OFFICE = "Morgan Family Office"
OPERATOR = "Beltway Executive Sedans"
RILEY = {"name": "Riley Chen", "email": "dispatch@beltwayexec.example.com", "phone": "+12025550144"}
AVERY = {"name": "Avery Park", "email": "accounts@beltwayexec.example.com", "phone": "+12025550145"}


def wedding_confirm_72h_company_billing_contact() -> TouchPoint:
    """No per-order billing contact: the booker's company's billing contact (Pat) gets it."""
    _, trips = _wedding_booked(billing=None, company=_company(MORGAN_OFFICE, billing=PAT))
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trips[0]))


def wedding_confirm_72h_no_billing_contact() -> TouchPoint:
    """No billing contact anywhere: the booker (Casey) gets it."""
    _, trips = _wedding_booked(billing=None)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trips[0]))


def legacy_wedding_leg_confirm_72h() -> TouchPoint:
    """Wedding-builder legs (leg ids, no Wedding service type) still count as a wedding."""
    _, trips = _wedding_booked(legacy=True)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trips[0]))


def legacy_wedding_leg_thank_you() -> TouchPoint:
    lead, _ = _wedding_booked(legacy=True)
    _finished(lead, WEDDING_DAY)
    return ready(_message(K.THANK_YOU, lead=lead))


def _wedding_day_confirmed() -> tuple[Lead, list[Reservation]]:
    lead, trips = _wedding_booked()
    advance(SETUP + timedelta(days=1))
    confirm_trip_day(wedding_day_group(lead, WEDDING_DAY))
    return lead, trips


def wedding_confirm_48h_confirmed_fees_owed() -> TouchPoint:
    """Day confirmed, fee acknowledgment still owed: the 48h notice still goes to Pat."""
    _, trips = _wedding_day_confirmed()
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trips[0]))


def wedding_confirm_48h_confirmed_fees_acknowledged() -> TouchPoint:
    lead, trips = _wedding_day_confirmed()
    lead_services.acknowledge_fees(lead, ip="203.0.113.7")
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trips[0]))


def gnet_assignment_no_affiliate_confirmation() -> TouchPoint:
    """GNet coverage gets no affiliate confirmation: at T-48h only the customer is sent to."""
    _, trip = _plain_booked()
    offer = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(**VENDOR),
        status=Assignment.Status.OFFERED,
        channel=Assignment.Channel.GNET,
        payout=Decimal("140.00"),
    )
    dispatch_services.confirm(offer)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trip))


def in_house_assignment_no_affiliate_confirmation() -> TouchPoint:
    """In-house coverage gets no affiliate confirmation either."""
    _, trip = _plain_booked()
    driver = DriverFactory(
        name="Alex Romero", phone="+12025550166", email="alex.romero@allprocharter.com"
    )
    dispatch_services.assign_in_house(trip, driver)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER_2, trip=trip))


def farm_in_confirm_72h() -> TouchPoint:
    """An operator (affiliate account) farms a trip in: the 72h goes to the operator."""
    _, trip = _plain_booked(RILEY, company=_company(OPERATOR, operator=True))
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trip))


def farm_in_wedding_confirm_72h() -> TouchPoint:
    """Not in build(): today this goes to the operator's billing contact, not the operator
    (spec section 4 says operator only). Pinned by a later task's ordinary test."""
    operator = _company(OPERATOR, billing=AVERY, operator=True)
    _, trips = _wedding_booked(person=RILEY, billing=None, company=operator)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trips[0]))


def utc_boundary_pair_one_message() -> TouchPoint:
    """Los Angeles, 10 AM and 8:30 PM PDT the same local day (the second is the next day in
    UTC): one message covers both."""
    morning, _ = _two_orders_same_day(later=time(20, 30), zone=PACIFIC, route=LA_ROUTE)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=morning))


def tp1_welcome_pacific() -> TouchPoint:
    lead = _order(DANA)
    _trip(lead, PLAIN_DAY, time(14, 0), zone=PACIFIC, route=LA_ROUTE)
    _lead_came_in(lead)
    return ready(_message(K.TP1_WELCOME, lead=lead))


def tp5_viewed_email_pacific() -> TouchPoint:
    lead = _viewed_an_hour_later(zone=PACIFIC, route=LA_ROUTE)
    return ready(_message(K.TP5_VIEWED_EMAIL, lead=lead))


def review_request_held_by_low_rating() -> TouchPoint:
    lead, _ = _plain_booked()
    _finished(lead, PLAIN_DAY)
    advance(local(PLAIN_DAY, time(19, 30)))
    lead_services.record_feedback(lead, rating=2, comment="The car was not clean.")
    return ready(_message(K.REVIEW_REQUEST, lead=lead))


def review_request_no_phone() -> TouchPoint:
    lead, _ = _plain_booked(DANA | {"phone": ""})
    _finished(lead, PLAIN_DAY)
    return ready(_message(K.REVIEW_REQUEST, lead=lead))


def trip_confirm_customer_phone_only() -> TouchPoint:
    """No email on file: the 72h goes by text only."""
    _, trip = _plain_booked(DANA | {"email": ""})
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=trip))


def _quote_followup_after(**state) -> TouchPoint:
    """Quote sent; an hour later the order moves on; the 24h follow-up is then due."""
    lead = _quoted()
    advance(SETUP + timedelta(hours=1))
    _force(lead, **state)
    return ready(_message(K.TP6_QUOTE_FOLLOWUP, lead=lead))


def quote_followup_skipped_lead_booked() -> TouchPoint:
    return _quote_followup_after(status=Lead.Status.BOOKED)


def quote_followup_skipped_lead_engaged() -> TouchPoint:
    return _quote_followup_after(status=Lead.Status.ENGAGED)


def quote_followup_skipped_lead_lost() -> TouchPoint:
    return _quote_followup_after(status=Lead.Status.LOST)


def quote_followup_skipped_deposit_taken() -> TouchPoint:
    lead = _quoted()
    advance(SETUP + timedelta(hours=1))
    PaymentPlan.objects.filter(lead=lead).update(
        deposit_status=PaymentPlan.DepositStatus.AUTHORIZED
    )
    return ready(_message(K.TP6_QUOTE_FOLLOWUP, lead=lead))


def thank_you_wedding_name_only() -> TouchPoint:
    """A plain trip on an order with a wedding name: the wedding thank-you."""
    lead, _ = _plain_booked()
    _force(lead, wedding_name="Morgan & Okafor Wedding")
    _finished(lead, PLAIN_DAY)
    return ready(_message(K.THANK_YOU, lead=lead))


def cancelled_sibling_dropped_from_grouped_sheet() -> TouchPoint:
    """Dana's 4 PM trip is cancelled: the 10 AM notice lists only the 10 AM trip."""
    morning, afternoon = _two_orders_same_day()
    advance(SETUP + timedelta(days=1))
    set_trip_status(afternoon, Reservation.TripStatus.CANCELLED)
    return ready(_message(K.TRIP_CONFIRM_CUSTOMER, trip=morning))


def build() -> dict[str, Callable[[], TouchPoint]]:
    """Every scenario by name. Names are the keys of expected.json."""
    return {
        fn.__name__: fn
        for fn in (
            tp1_welcome,
            tp2_lead_followup,
            tp3_quote_sent_sms,
            tp4_viewed_sms,
            tp5_viewed_email,
            tp6_quote_followup,
            tp7_expiring,
            tp8_expired,
            order_auth_expired,
            order_confirmed,
            order_cancelled,
            payment_reminder,
            trip_confirm_customer,
            trip_confirm_customer_2,
            trip_confirm_affiliate,
            driver_released,
            driver_updated,
            status_dispatched,
            status_on_the_way,
            status_arrived,
            wed_final_details,
            thank_you,
            review_request,
            wedding_confirm_72h_to_billing_contact,
            wedding_confirm_48h_to_billing_contact,
            wedding_thank_you,
            two_trips_same_day_one_message,
            two_trips_same_day_second_stands_down,
            wedding_and_plain_same_day_two_messages,
            trip_confirm_customer_2_day_confirmed,
            driver_released_pacific_trip,
            payment_reminder_account_order,
            review_request_held_by_complaint,
            wedding_confirm_72h_company_billing_contact,
            wedding_confirm_72h_no_billing_contact,
            legacy_wedding_leg_confirm_72h,
            legacy_wedding_leg_thank_you,
            wedding_confirm_48h_confirmed_fees_owed,
            wedding_confirm_48h_confirmed_fees_acknowledged,
            gnet_assignment_no_affiliate_confirmation,
            in_house_assignment_no_affiliate_confirmation,
            farm_in_confirm_72h,
            utc_boundary_pair_one_message,
            tp1_welcome_pacific,
            tp5_viewed_email_pacific,
            review_request_held_by_low_rating,
            review_request_no_phone,
            trip_confirm_customer_phone_only,
            quote_followup_skipped_lead_booked,
            quote_followup_skipped_lead_engaged,
            quote_followup_skipped_lead_lost,
            quote_followup_skipped_deposit_taken,
            thank_you_wedding_name_only,
            cancelled_sibling_dropped_from_grouped_sheet,
        )
    }
