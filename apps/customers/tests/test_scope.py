"""Which trips a customer may see, the label each one wears, and the row a page renders."""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.http import Http404
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.addresses.factories import AirportFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.customers import selectors
from apps.customers.factories import CustomerAccountFactory
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.payments import services as payment_services
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.payments.trip_money import money_for
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import (
    HourlyReservationFactory,
    PassengerFactory,
    ReservationFactory,
    StopFactory,
)
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
REFUND = "apps.payments.services.stripe.Refund.create"


def _admin() -> User:
    return UserFactory(role=User.Role.OWNER_ADMIN)


def _visible(account) -> set[int]:
    return set(selectors.visible_trips(account).values_list("pk", flat=True))


# --- scope ---------------------------------------------------------------------------------


def test_booking_contact_and_billing_contact_both_see_the_trip(account):
    booked_by_them = ReservationFactory(lead=LeadFactory(contact=account.contact))
    billed_to_them = ReservationFactory(lead=LeadFactory(billing_contact=account.contact))
    ReservationFactory()  # someone else's

    assert _visible(account) == {booked_by_them.pk, billed_to_them.pk}


def test_other_customers_trip_is_404(account, make_trip):
    mine = make_trip(account)
    theirs = make_trip(CustomerAccountFactory())

    assert selectors.trip_or_404(account, mine.pk) == mine
    with pytest.raises(Http404):
        selectors.trip_or_404(account, theirs.pk)
    with pytest.raises(Http404):
        selectors.trip_or_404(account, theirs.pk + 1000)


def test_wedding_lead_is_hidden_even_with_trips_built(account):
    wedding = ServiceTypeFactory(name=WEDDING_SERVICE_NAME)
    lead = LeadFactory(contact=account.contact, status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, service_type=wedding)
    ReservationFactory(lead=lead)  # a plain shuttle on the same wedding order
    legacy = LeadFactory(contact=account.contact)
    ReservationFactory(lead=legacy, source_leg_id="guests-in")

    assert lead.is_wedding and legacy.is_wedding
    assert _visible(account) == set()


def test_farm_in_lead_is_hidden(make_trip):
    operator = ContactFactory(company=CompanyFactory(is_affiliate_account=True))
    account = CustomerAccountFactory(contact=operator)
    make_trip(account)

    assert _visible(account) == set()


def test_scope_filter_agrees_with_model_properties():
    account = CustomerAccountFactory(contact=ContactFactory(company=CompanyFactory()))
    me = account.contact
    affiliate = ContactFactory(company=CompanyFactory(is_affiliate_account=True))
    odd_wedding = ServiceTypeFactory(name=f"  {WEDDING_SERVICE_NAME.upper()} ")
    transfer = ServiceTypeFactory(name="Airport Transfer")

    plain = LeadFactory(contact=me)
    ReservationFactory.create_batch(2, lead=plain, service_type=transfer)
    ReservationFactory(lead=LeadFactory(contact=ContactFactory(), billing_contact=me))
    ReservationFactory(lead=LeadFactory(contact=me, billing_contact=me))
    mixed_wedding = LeadFactory(contact=me)
    ReservationFactory(lead=mixed_wedding, service_type=odd_wedding)
    ReservationFactory(lead=mixed_wedding, service_type=transfer, trip_status=S.CANCELLED)
    ReservationFactory(lead=LeadFactory(contact=me), source_leg_id="early-out")
    ReservationFactory(lead=LeadFactory(contact=affiliate, billing_contact=me))
    ReservationFactory(lead=LeadFactory(contact=ContactFactory()))

    expected = {
        t.pk
        for t in Reservation.objects.select_related("lead__contact__company")
        if me.pk in (t.lead.contact_id, t.lead.billing_contact_id)
        and not t.lead.is_wedding
        and not t.lead.is_farm_in
    }
    assert len(expected) == 4  # the mix holds both kinds, not an empty agreement
    assert _visible(account) == expected


# --- labels --------------------------------------------------------------------------------


def test_labels_are_the_spec_table():
    assert {key: text for key, (text, _tone) in selectors.LABELS.items()} == {
        "request_received": "Request received",
        "quote_ready": "Quote ready",
        "quote_expired": "Quote expired",
        "confirming": "Confirming",
        "confirmed": "Confirmed",
        "driver_assigned": "Driver assigned",
        "in_progress": "In progress",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "no_show": "No-show",
        "closed": "Closed",
    }
    assert {tone for _text, tone in selectors.LABELS.values()} <= {
        "info",
        "gold",
        "ok",
        "mute",
        "danger",
    }


@pytest.mark.parametrize(
    "lead_status,trip_status,expected",
    [
        (Lead.Status.NEW, S.PENDING, "request_received"),
        (Lead.Status.QUOTED, S.PENDING, "quote_ready"),
        (Lead.Status.ENGAGED, S.PENDING, "confirming"),
        (Lead.Status.BOOKED, S.UNASSIGNED, "confirmed"),
        (Lead.Status.BOOKED, S.DISPATCHED, "driver_assigned"),
        (Lead.Status.BOOKED, S.ON_THE_WAY, "in_progress"),
        (Lead.Status.BOOKED, S.DONE, "completed"),
        (Lead.Status.BOOKED, S.LATE_CANCEL, "cancelled"),
        (Lead.Status.BOOKED, S.NO_SHOW, "no_show"),
        (Lead.Status.LOST, S.PENDING, "closed"),
        # The rest of the table's rows, beyond the brief's ten.
        (Lead.Status.BOOKED, S.PENDING, "confirmed"),
        (Lead.Status.BOOKED, S.ASSIGNED, "confirmed"),
        (Lead.Status.BOOKED, "", "confirmed"),
        (Lead.Status.BOOKED, S.ARRIVED, "in_progress"),
        (Lead.Status.BOOKED, S.CUSTOMER_IN_CAR, "in_progress"),
        (Lead.Status.BOOKED, S.CANCELLED, "cancelled"),
        (Lead.Status.QUOTED, S.CANCELLED, "cancelled"),
    ],
)
def test_label_key(lead_status, trip_status, expected):
    trip = ReservationFactory(lead__status=lead_status, trip_status=trip_status)
    assert selectors.label_key(trip) == expected


@pytest.mark.parametrize(
    "booked_total,trip_status,expected",
    [
        # Never booked: no trip total was ever frozen. Closed, whatever the trip says.
        (None, S.PENDING, "closed"),
        (None, S.CANCELLED, "closed"),
        # Lost after booking (the order was cancelled): the trip's own end shows.
        (Decimal("185.00"), S.CANCELLED, "cancelled"),
        (Decimal("185.00"), S.LATE_CANCEL, "cancelled"),
        (Decimal("185.00"), S.NO_SHOW, "no_show"),
        (Decimal("185.00"), S.DONE, "completed"),
        (Decimal("0.00"), S.CANCELLED, "cancelled"),  # added after booking
    ],
)
def test_lost_order_label(booked_total, trip_status, expected):
    trip = ReservationFactory(
        lead__status=Lead.Status.LOST, booked_total=booked_total, trip_status=trip_status
    )
    assert selectors.label_key(trip) == expected


def _booked(account, n: int = 2) -> Lead:
    """An order booked through the real staff path, with `n` trips ten days out."""
    when = timezone.now() + timedelta(days=10)
    lead = LeadFactory(contact=account.contact, status=Lead.Status.QUOTED)
    ReservationFactory.create_batch(n, lead=lead, pickup_date=when.date(), pickup_time=time(9, 0))
    lead_services.book_lead(lead)
    return lead


def _labels(account) -> list[tuple[str, str]]:
    trips = list(selectors.visible_trips(account).order_by("pk"))
    rows = selectors.trip_rows(trips)
    return [(row["label"], selectors.bucket(trip)) for row, trip in zip(rows, trips, strict=True)]


def test_a_booked_order_cancelled_outright_reads_cancelled_in_past(account):
    lead = _booked(account)
    with patch(REFUND, side_effect=lambda **kw: MagicMock(id="re_1")):
        payment_services.cancel_trips(
            lead, list(lead.reservations.all()), user=_admin(), reason="Customer cancelled"
        )
    lead.refresh_from_db()

    assert lead.status == Lead.Status.LOST
    assert _labels(account) == [("Cancelled", "past"), ("Cancelled", "past")]


def test_a_no_show_on_a_cancelled_order_stays_a_no_show(account):
    lead = _booked(account)
    no_show, rest = lead.reservations.order_by("pk")
    set_trip_status(no_show, S.NO_SHOW)
    with patch(REFUND, side_effect=lambda **kw: MagicMock(id="re_1")):
        payment_services.cancel_trips(lead, [rest], user=_admin(), reason="Customer cancelled")
    lead.refresh_from_db()

    assert lead.status == Lead.Status.LOST  # the last live trip went, so the order did
    assert _labels(account) == [("No-show", "past"), ("Cancelled", "past")]


def test_a_quote_marked_lost_by_staff_is_closed(account):
    lead = LeadFactory(contact=account.contact, status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead)
    lead_services.mark_lost(lead, "")

    assert _labels(account) == [("Closed", "requests")]


def test_a_released_deposit_was_never_booked_so_it_is_closed(account):
    lead = LeadFactory(contact=account.contact, status=Lead.Status.ENGAGED)
    ReservationFactory(lead=lead)
    ChargeFactory(
        plan=PaymentPlanFactory(lead=lead),
        status=Charge.Status.AUTHORIZED,
        stripe_payment_intent_id="pi_held",
    )
    with patch("apps.payments.services.stripe.PaymentIntent.cancel"):
        payment_services.cancel_order(lead)
    lead.refresh_from_db()

    assert lead.status == Lead.Status.LOST
    assert _labels(account) == [("Closed", "requests")]


def test_expired_quote_is_quote_expired():
    now = timezone.now()
    trip = ReservationFactory(
        lead__status=Lead.Status.QUOTED, lead__quote_expires_at=now + timedelta(days=1)
    )
    assert selectors.label_key(trip) == "quote_ready"
    with patch("django.utils.timezone.now", return_value=now + timedelta(days=2)):
        assert selectors.label_key(trip) == "quote_expired"


@pytest.mark.parametrize(
    "key,expected",
    [
        ("request_received", "requests"),
        ("quote_expired", "requests"),
        ("closed", "requests"),
        ("completed", "past"),
        ("cancelled", "past"),
        ("no_show", "past"),
        ("quote_ready", "upcoming"),
        ("confirming", "upcoming"),
        ("confirmed", "upcoming"),
        ("driver_assigned", "upcoming"),
        ("in_progress", "upcoming"),
    ],
)
def test_bucket(key, expected):
    with patch.object(selectors, "label_key", return_value=key):
        assert selectors.bucket(ReservationFactory.build()) == expected
    assert expected in selectors.BUCKETS


# --- the row -------------------------------------------------------------------------------


ROW_KEYS = {
    "pk",
    "date",
    "time",
    "zone",
    "route",
    "passenger",
    "confirmation",
    "confirmation_label",
    "label",
    "tone",
    "total",
    "reference",
    "has_receipt",
}


def test_trip_row(account, make_trip):
    trip = make_trip(
        account,
        pickup_date=date(2026, 11, 18),
        pickup_time=time(6, 15),
        pickup_timezone="America/New_York",
        lead__status=Lead.Status.BOOKED,
        stops=[],
    )
    StopFactory(reservation=trip, sequence=0, address="2200 Clarendon Blvd, Arlington, VA 22201")
    StopFactory(
        reservation=trip,
        sequence=1,
        address="1 Saarinen Cir, Dulles, VA 20166",
        airport=AirportFactory(iata="IAD", city="Dulles"),
    )
    PassengerFactory(reservation=trip, name="Mia Chen", sort=0)
    PassengerFactory(reservation=trip, name="Dana Whitfield", is_lead=True, sort=1)

    row = selectors.trip_row(selectors.visible_trips(account).get())

    assert set(row) == ROW_KEYS
    assert row == {
        "pk": trip.pk,
        "date": "Wed, Nov 18",
        "time": "6:15 AM",
        "zone": "EST",
        "route": "Arlington, VA to Dulles (IAD)",
        "passenger": "Dana Whitfield",
        "confirmation": trip.reference,
        "confirmation_label": trip.reference,
        "label": "Confirmed",
        "tone": "ok",
        "total": trip.line_total,
        "reference": "",
        "has_receipt": False,
    }


def test_trip_row_shows_the_limoanywhere_confirmation_and_the_trip_zone(account, make_trip):
    trip = make_trip(
        account,
        la_confirmation="48213",
        pickup_date=date(2026, 7, 4),
        pickup_time=time(15, 5),
        pickup_timezone="America/Los_Angeles",
    )
    row = selectors.trip_row(trip)
    assert row["confirmation"] == "48213"
    assert row["confirmation_label"] == "Conf. 48213"
    assert (row["date"], row["time"], row["zone"]) == ("Sat, Jul 4", "3:05 PM", "PDT")


@pytest.mark.parametrize(
    "hours,min_hours,expected",
    [(4, 2, "Hourly, 4 hours"), (0, 3, "Hourly, 3 hours"), (1, 0, "Hourly, 1 hour")],
)
def test_hourly_route_names_the_hours(account, make_trip, hours, min_hours, expected):
    trip = make_trip(account, factory=HourlyReservationFactory, hours=hours, min_hours=min_hours)
    assert selectors.trip_row(trip)["route"] == expected


def test_thin_trip_row_renders():
    trip = ReservationFactory(
        vehicle=None, rate=0, pickup_date=None, pickup_time=None, pickup_timezone="", stops=[]
    )
    row = selectors.trip_row(trip)  # not from visible_trips: nothing prefetched
    assert row["total"] is None
    assert row["zone"] in ("EST", "EDT")  # the business zone, America/New_York
    assert row["route"] == "Route to be confirmed"
    assert row["date"] == "Date to be confirmed"
    assert row["time"] == "Time to be confirmed"
    assert row["passenger"] == ""


def test_row_never_carries_vendor_or_internal_fields(account, make_trip):
    trip = make_trip(account, affiliate_cost=120, cost_ratio_pct=65)
    row = selectors.trip_row(trip)
    assert set(row) == ROW_KEYS
    assert Decimal("120") not in row.values()


def test_rows_cost_a_fixed_number_of_queries(account, make_trip, django_assert_num_queries):
    airport = AirportFactory(iata="DCA", city="Washington")
    statuses = [S.PENDING, S.ASSIGNED, S.DISPATCHED, S.ON_THE_WAY, S.DONE]
    for i in range(10):
        trip = make_trip(
            account,
            trip_status=statuses[i % len(statuses)],
            pickup_date=date(2026, 12, 1) + timedelta(days=i),
            pickup_time=time(9, 0),
            pickup_timezone="America/New_York",
            lead__status=Lead.Status.BOOKED,
            stops=["100 Main St, Bethesda, MD 20814"],
        )
        StopFactory(reservation=trip, sequence=1, address="DCA", airport=airport)
        PassengerFactory(reservation=trip, is_lead=True)

    # trips (with lead, payment, vehicle), their stops (with airports), their passengers
    with django_assert_num_queries(3):
        rows = selectors.trip_rows(selectors.visible_trips(account))
    assert len(rows) == 10
    assert all(r["route"] == "Bethesda, MD to Washington (DCA)" for r in rows)


def _order_with_money(account, deposit: str) -> tuple[Lead, Charge]:
    lead = LeadFactory(contact=account.contact, status=Lead.Status.BOOKED)
    plan = PaymentPlanFactory(lead=lead)
    deposit_row = ChargeFactory(plan=plan, amount=Decimal(deposit), status=Charge.Status.SUCCEEDED)
    return lead, deposit_row


def _cancelled(lead, status=S.LATE_CANCEL, days: int = 1) -> Reservation:
    return ReservationFactory(
        lead=lead,
        rate=185,
        booked_total=Decimal("185.00"),
        trip_status=status,
        pickup_date=date(2026, 12, 1) + timedelta(days=days),
    )


def test_cancelled_rows_cost_a_fixed_number_of_queries_per_order(
    account, make_trip, django_assert_num_queries
):
    for _ in range(5):
        make_trip(account, trip_status=S.DONE, lead__status=Lead.Status.BOOKED)
    first, _ = _order_with_money(account, "300.00")
    a = [_cancelled(first, days=1), _cancelled(first, days=2), _cancelled(first, S.CANCELLED)]
    ReservationFactory(lead=first, booked_total=Decimal("185.00"), trip_status=S.UNASSIGNED)
    second, deposit = _order_with_money(account, "300.00")
    charged, refunded = _cancelled(second, days=1), _cancelled(second, days=2)
    ChargeFactory(
        plan=deposit.plan,
        kind=Charge.Kind.TRIP,
        reservation=charged,
        amount=Decimal("20.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ChargeFactory(
        plan=deposit.plan,
        kind=Charge.Kind.REFUND,
        reservation=refunded,
        amount=Decimal("10.00"),
        status=Charge.Status.SUCCEEDED,
    )

    # 3 for the rows, 1 for every cancelled trip's own charges, and per order that has a
    # cancelled trip one allocation: its two prepaid sums, frozen shares, sibling trips.
    with django_assert_num_queries(3 + 1 + 2 * 4):
        rows = selectors.trip_rows(selectors.visible_trips(account))
    totals = {r["pk"]: r["total"] for r in rows}
    for trip in [*a, charged, refunded]:
        assert totals[trip.pk] == money_for(trip).allocated
    assert [totals[t.pk] for t in (*a, charged, refunded)] == [
        Decimal("185.00"),
        Decimal("115.00"),
        Decimal("0.00"),
        Decimal("205.00"),  # 185 prepaid + its own 20 charge
        Decimal("105.00"),  # the remaining 115 prepaid less its own 10 refund
    ]

    a += [_cancelled(first, days=5), _cancelled(first, S.CANCELLED, days=6)]
    with django_assert_num_queries(3 + 1 + 2 * 4):
        rows = selectors.trip_rows(selectors.visible_trips(account))
    assert len(rows) == 5 + 4 + 2 + 2


# --- totals --------------------------------------------------------------------------------


def test_cancelled_trip_total_is_amount_kept(account):
    lead = LeadFactory(contact=account.contact, status=Lead.Status.BOOKED)
    plan = PaymentPlanFactory(lead=lead)
    ChargeFactory(
        plan=plan,
        kind=Charge.Kind.DEPOSIT,
        amount=Decimal("100.00"),
        status=Charge.Status.SUCCEEDED,
    )
    late = ReservationFactory(
        lead=lead, rate=185, booked_total=Decimal("185.00"), trip_status=S.LATE_CANCEL
    )
    plain = ReservationFactory(
        lead=lead, rate=185, booked_total=Decimal("185.00"), trip_status=S.CANCELLED
    )

    assert selectors.display_total(late) == money_for(late).allocated == Decimal("100.00")
    assert selectors.display_total(plain) == money_for(plain).allocated == Decimal("0.00")
    assert selectors.trip_row(late)["total"] == Decimal("100.00")


def test_display_total_is_the_line_total_or_none_when_unpriced():
    assert selectors.display_total(ReservationFactory(rate=185)) == Decimal("185.00")
    assert selectors.display_total(ReservationFactory(rate=0)) is None
