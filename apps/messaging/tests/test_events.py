"""Queueing messages (spec C sections 3, 5 and 9): events, date rows, waiting rows.

The parity tests run the old scheduler (`touchpoints`) and `events` on the same order, each
inside a rolled-back savepoint on a frozen clock, and compare the TouchPoints each leaves:
(kind, trip, scheduled_for, status). Task 11 swaps the callers on the strength of these.
"""

import itertools
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.messaging import events, overrides, sender, touchpoints
from apps.messaging.factories import MessageRowFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, NotificationConfig, TouchPoint
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

K = TouchPoint.Kind
S = TouchPoint.Status
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
NEVER = datetime.min.replace(tzinfo=UTC)
EASTERN = ZoneInfo("America/New_York")
SEND = "apps.integrations.podium.send_message"
DRIVER = {
    "driver_name": "Sam Rivera",
    "driver_cell": "+15715551212",
    "vehicle_desc": "Suburban",
    "vehicle_number": "9",
}


@pytest.fixture
def clock():
    """Freeze `timezone.now` at NOW; `.now` can be moved."""

    class Clock:
        now = NOW

    with patch("django.utils.timezone.now", side_effect=lambda: Clock.now):
        yield Clock


@pytest.fixture
def live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"


def _ok(**_):
    return {"uid": "msg-1"}


# --- builders --------------------------------------------------------------------------


_n = itertools.count(1)


def _lead(status=Lead.Status.BOOKED, **kw) -> Lead:
    n = next(_n)
    contact = ContactFactory(
        name="Bea Booker", phone=f"+1202555{n:04d}", email=f"bea{n}@example.com"
    )
    return LeadFactory(status=status, contact=contact, **kw)


def _trip(lead, *, hours=24 * 10, wedding=False, **kw) -> Reservation:
    """A trip `hours` from the clock, on the Eastern clock."""
    when = (timezone.now() + timedelta(hours=hours)).astimezone(EASTERN)
    if wedding:
        kw["service_type"] = ServiceTypeFactory(name=WEDDING_SERVICE_NAME)
    return ReservationFactory(
        lead=lead,
        pickup_date=when.date(),
        pickup_time=when.time().replace(microsecond=0),
        pickup_timezone="America/New_York",
        **kw,
    )


def _farm_out(trip, **kw) -> Assignment:
    return AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, **kw)


def _dispatched(trip, **kw) -> Assignment:
    assignment = _farm_out(trip, **{**DRIVER, **kw})
    trip.trip_status = Reservation.TripStatus.DISPATCHED
    trip.save(update_fields=["trip_status", "updated_at"])
    return assignment


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _added(**kw) -> MessageTemplate:
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("sms_body", "Text for {first_name}")
    kw.setdefault("send_email", False)
    return MessageRowFactory(**kw)


def _switch_on(*keys: str) -> None:
    MessageTemplate.objects.filter(key__in=keys).update(enabled=True)


def _tps(lead, **kw):
    return TouchPoint.objects.filter(lead=lead, **kw)


# --- parity harness --------------------------------------------------------------------


def _state(lead_pk: int, kinds=None) -> list[tuple]:
    qs = TouchPoint.objects.filter(lead_id=lead_pk)
    if kinds is not None:
        qs = qs.filter(kind__in=kinds)
    return sorted(
        (tp.kind, tp.reservation_id or 0, tp.scheduled_for or NEVER, tp.status) for tp in qs
    )


def _run(lead: Lead, steps, kinds=None) -> list[tuple]:
    """Play `steps(lead)` in a savepoint that is rolled back; the TouchPoints it left."""
    with transaction.atomic():
        steps(Lead.objects.get(pk=lead.pk))
        got = _state(lead.pk, kinds)
        transaction.set_rollback(True)
    return got


def _parity(lead: Lead, old, new, kinds=None) -> tuple[list, list]:
    return _run(lead, old, kinds), _run(lead, new, kinds)


def _trip_of(lead: Lead, pk: int) -> Reservation:
    return lead.reservations.get(pk=pk)


# --- service-type filter (spec section 3) ----------------------------------------------


def test_a_trip_row_matches_its_own_trip_type(clock):
    charter = ServiceTypeFactory(name="Charter")
    row = _added(trigger="pickup", timing_amount=24, timing_unit="hours", timing_direction="before")
    row.service_types.set([charter])
    lead = _lead()
    mine = _trip(lead, service_type=charter)
    other = _trip(lead)

    events.schedule_dates(lead)

    assert list(_tps(lead, message=row).values_list("reservation", flat=True)) == [mine.pk]
    assert not _tps(lead, message=row, reservation=other).exists()


def test_an_order_row_matches_when_any_live_trip_has_the_type(clock):
    charter = ServiceTypeFactory(name="Charter")
    row = _added(trigger="deposit_paid")
    row.service_types.set([charter])
    matching, cancelled_only, none = _lead(), _lead(), _lead()
    _trip(matching)
    _trip(matching, service_type=charter)
    _trip(cancelled_only, service_type=charter, trip_status=Reservation.TripStatus.CANCELLED)
    _trip(none)

    for lead in (matching, cancelled_only, none):
        events.emit("deposit_paid", lead)

    assert _tps(matching, message=row).count() == 1
    assert not _tps(cancelled_only, message=row).exists()
    assert not _tps(none, message=row).exists()


def test_the_wedding_type_matches_a_legacy_wedding_leg(clock):
    row = _added(trigger="pickup", timing_amount=1, timing_unit="days", timing_direction="before")
    row.service_types.set([ServiceTypeFactory(name=WEDDING_SERVICE_NAME)])
    lead = _lead()
    leg = _trip(lead, source_leg_id="guests-in")

    events.schedule_dates(lead)

    assert _tps(lead, message=row, reservation=leg).exists()


# --- emit --------------------------------------------------------------------------------


def test_emit_queues_each_enabled_live_row_with_its_message(clock):
    off = _added(trigger="lead_created", enabled=False)
    archived = _added(trigger="lead_created", archived_at=NOW)
    lead = _lead(Lead.Status.NEW)

    made = events.emit("lead_created", lead)

    assert {(tp.kind, tp.message.key, tp.scheduled_for) for tp in made} == {
        ("tp1_welcome", "tp1_welcome", NOW + timedelta(minutes=30)),
        ("tp2_lead_followup", "tp2_lead_followup", NOW + timedelta(hours=2)),
    }
    assert all(tp.reservation_id is None and tp.status == S.SCHEDULED for tp in made)
    assert not _tps(lead, message__in=[off, archived]).exists()


def test_a_trip_event_needs_its_trip():
    with pytest.raises(ValueError):
        events.emit("status_arrived", _lead())


def test_an_order_event_does_not_carry_the_trip(clock):
    row = _added(trigger="deposit_paid")
    lead = _lead()
    trip = _trip(lead)

    (tp,) = events.emit("deposit_paid", lead, trip)

    assert tp.message == row and tp.reservation_id is None


def test_nothing_is_queued_with_the_master_switch_off(clock):
    NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})
    lead = _lead()
    trip = _trip(lead)

    assert events.emit("lead_created", lead) == []
    events.schedule_dates(lead)
    assert not _tps(lead).exists()
    assert trip.pk


def test_an_unknown_trigger_is_an_error():
    with pytest.raises(KeyError):
        events.emit("no_such_trigger", _lead())


def test_what_is_due_goes_out_on_commit(live, django_capture_on_commit_callbacks):
    row = _added(trigger="deposit_paid")
    lead = _lead()

    with patch(SEND, side_effect=_ok) as send, django_capture_on_commit_callbacks(execute=True):
        (tp,) = events.emit("deposit_paid", lead)

    tp.refresh_from_db()
    assert tp.status == S.SENT and tp.message == row
    assert send.call_args.kwargs["body"] == "Text for Bea"


def test_a_later_row_is_left_for_the_cron(live, django_capture_on_commit_callbacks):
    lead = _lead(Lead.Status.NEW)

    with patch(SEND, side_effect=_ok) as send, django_capture_on_commit_callbacks() as callbacks:
        events.emit("lead_created", lead)

    send.assert_not_called()
    assert callbacks == []
    assert set(_tps(lead).values_list("status", flat=True)) == {S.SCHEDULED}


# --- idempotency -------------------------------------------------------------------------


def test_once_blocks_on_anything_but_a_cancelled_row(clock):
    lead = _lead(Lead.Status.QUOTED)
    events.emit("quote_viewed", lead)
    events.emit("quote_viewed", lead)
    assert _tps(lead, kind=K.TP4_VIEWED_SMS).count() == 1

    _tps(lead, kind=K.TP4_VIEWED_SMS).update(status=S.SKIPPED)
    _tps(lead, kind=K.TP5_VIEWED_EMAIL).update(status=S.CANCELLED)
    events.emit("quote_viewed", lead)

    assert _tps(lead, kind=K.TP4_VIEWED_SMS).count() == 1
    assert _tps(lead, kind=K.TP5_VIEWED_EMAIL, status=S.SCHEDULED).count() == 1


def test_an_old_scheduler_row_counts_as_the_rows_own(clock):
    lead = _lead(Lead.Status.QUOTED)
    TouchPointFactory(lead=lead, kind=K.TP4_VIEWED_SMS, message=None, scheduled_for=NOW)

    events.emit("quote_viewed", lead)

    assert _tps(lead, kind=K.TP4_VIEWED_SMS).count() == 1


def test_a_second_lapse_is_told_again(clock):
    lead = _lead(Lead.Status.QUOTED)
    events.emit("deposit_hold_released", lead)
    events.emit("deposit_hold_released", lead)
    assert _tps(lead, kind=K.ORDER_AUTH_EXPIRED).count() == 1

    _tps(lead, kind=K.ORDER_AUTH_EXPIRED).update(status=S.SENT)
    events.emit("deposit_hold_released", lead)

    assert _tps(lead, kind=K.ORDER_AUTH_EXPIRED).count() == 2


def test_a_held_review_request_is_never_queued_again(clock):
    lead = _lead()
    _trip(lead, trip_status=Reservation.TripStatus.DONE)
    events.emit("all_trips_done", lead)
    _tps(lead, kind=K.REVIEW_REQUEST).update(status=S.CANCELLED)

    events.emit("all_trips_done", lead)

    assert _tps(lead, kind=K.REVIEW_REQUEST).count() == 1
    assert _tps(lead, kind=K.THANK_YOU).count() == 1


def test_a_resent_quote_starts_its_program_over(clock):
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW, quote_expires_at=NOW + timedelta(days=3))
    events.emit("quote_sent", lead)
    _tps(lead, kind=K.TP3_QUOTE_SENT_SMS).update(status=S.SENT)
    first_followup = _tps(lead, kind=K.TP6_QUOTE_FOLLOWUP).get()

    clock.now = NOW + timedelta(hours=1)
    lead.quote_sent_at = clock.now
    lead.save(update_fields=["quote_sent_at"])
    events.emit("quote_sent", lead)

    first_followup.refresh_from_db()
    assert first_followup.status == S.CANCELLED
    assert _tps(lead, kind=K.TP3_QUOTE_SENT_SMS, status=S.SCHEDULED).get().scheduled_for == (
        clock.now + timedelta(minutes=3)
    )


def test_a_sent_expiry_nudge_is_not_repeated_for_the_same_expiry(clock):
    """Departure: today a quote re-sent with an unchanged expiry queues its expiry nudges
    again; a nudge already sent for that same expiry now blocks the repeat."""
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW, quote_expires_at=NOW + timedelta(days=3))
    events.emit("quote_sent", lead)
    _tps(lead, kind=K.TP7_EXPIRING).update(status=S.SENT)

    events.emit("quote_sent", lead)

    assert _tps(lead, kind=K.TP7_EXPIRING).count() == 1
    assert _tps(lead, kind=K.TP8_EXPIRED, status=S.SCHEDULED).count() == 1


def test_trip_events_are_one_per_trip(clock):
    _switch_on("status_on_the_way")
    lead = _lead()
    a, b = _trip(lead), _trip(lead)

    for trip in (a, b, a):
        events.emit("status_on_the_way", lead, trip)

    assert sorted(_tps(lead, kind=K.STATUS_ON_THE_WAY).values_list("reservation", flat=True)) == (
        sorted([a.pk, b.pk])
    )


def test_a_trip_cancelled_again_is_told_again(clock):
    row = _added(trigger="trip_cancelled")
    lead = _lead()
    trip = _trip(lead)
    events.emit("trip_cancelled", lead, trip)
    events.emit("trip_cancelled", lead, trip)
    assert _tps(lead, message=row).count() == 1

    _tps(lead, message=row).update(status=S.SENT)
    events.emit("trip_cancelled", lead, trip)

    assert _tps(lead, message=row).count() == 2


# --- parity: events ----------------------------------------------------------------------


def test_parity_lead_created(clock):
    lead = _lead(Lead.Status.NEW)
    old, new = _parity(
        lead, touchpoints.schedule_lead_created, lambda x: events.emit("lead_created", x)
    )
    assert old == new and len(new) == 2


def _quote_sent_new(lead):
    events.emit("quote_sent", lead)


@pytest.mark.parametrize(
    "sent_ago,expires_in",
    [
        (timedelta(0), timedelta(days=3)),
        (timedelta(0), timedelta(hours=1)),
        (timedelta(0), None),
        (timedelta(days=2), timedelta(days=3)),
    ],
    ids=["expires-later", "expiry-within-24h", "no-expiry", "reissued"],
)
def test_parity_quote_sent(clock, sent_ago, expires_in):
    """A reissued quote keeps its sent time, and its follow-ups count from it."""
    expires = NOW + expires_in if expires_in else None
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW - sent_ago, quote_expires_at=expires)

    old, new = _parity(lead, touchpoints.schedule_quote_sent, _quote_sent_new)

    assert old == new and new


def test_parity_quote_resent(clock):
    sent = NOW - timedelta(days=2)
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=sent, quote_expires_at=NOW + timedelta(days=1))
    with patch("django.utils.timezone.now", return_value=sent):
        touchpoints.schedule_quote_sent(lead)
        touchpoints.schedule_quote_viewed(lead)
    _tps(lead, kind__in=[K.TP3_QUOTE_SENT_SMS, K.TP4_VIEWED_SMS]).update(status=S.SENT)
    lead.quote_sent_at = NOW
    lead.quote_expires_at = NOW + timedelta(days=4)
    lead.save(update_fields=["quote_sent_at", "quote_expires_at"])

    old, new = _parity(lead, touchpoints.schedule_quote_sent, _quote_sent_new)

    assert old == new
    assert ("tp5_viewed_email", 0, sent + timedelta(hours=2), S.CANCELLED) in new


def test_parity_quote_viewed(clock):
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW)

    def old(x):
        touchpoints.schedule_quote_viewed(x)
        touchpoints.schedule_quote_viewed(x)
        touchpoints.schedule_quote_sent(x)  # a re-send withdraws the viewed nudges
        touchpoints.schedule_quote_viewed(x)

    def new(x):
        events.emit("quote_viewed", x)
        events.emit("quote_viewed", x)
        events.emit("quote_sent", x)
        events.emit("quote_viewed", x)

    got_old, got_new = _parity(lead, old, new)
    assert got_old == got_new and len(got_new) == 6


def test_parity_deposit_hold_released(clock):
    lead = _lead(Lead.Status.QUOTED)

    def steps(fire):
        def play(x):
            fire(x)
            fire(x)
            _tps(x).update(status=S.SENT)
            fire(x)

        return play

    old, new = _parity(
        lead,
        steps(touchpoints.trigger_auth_expired),
        steps(lambda x: events.emit("deposit_hold_released", x)),
    )
    assert old == new and len(new) == 2


@pytest.mark.parametrize("confirmed", [True, False])
def test_parity_order_decided(clock, confirmed):
    lead = _lead()
    trigger = "order_confirmed" if confirmed else "order_cancelled"

    def steps(fire):
        def play(x):
            fire(x)
            fire(x)
            _tps(x).update(status=S.SENT)
            fire(x)

        return play

    old, new = _parity(
        lead,
        steps(lambda x: touchpoints.trigger_order_decided(x, confirmed=confirmed)),
        steps(lambda x: events.emit(trigger, x)),
    )
    assert old == new and len(new) == 2


@pytest.mark.parametrize("status", ["on_the_way", "arrived", "assigned"])
def test_parity_status_messages(clock, status):
    _switch_on("status_on_the_way", "status_arrived")
    lead = _lead()
    trip = _trip(lead)

    def old(x):
        touchpoints.notify_status_change(_trip_of(x, trip.pk), status)
        touchpoints.notify_status_change(_trip_of(x, trip.pk), status)

    def new(x):
        if status != "assigned":  # no message is mapped to it
            events.emit(f"status_{status}", x, _trip_of(x, trip.pk))
            events.emit(f"status_{status}", x, _trip_of(x, trip.pk))

    got_old, got_new = _parity(lead, old, new)
    assert got_old == got_new


def test_parity_status_messages_off_by_default(clock):
    lead = _lead()
    trip = _trip(lead)
    old, new = _parity(
        lead,
        lambda x: touchpoints.notify_status_change(_trip_of(x, trip.pk), "arrived"),
        lambda x: events.emit("status_arrived", x, _trip_of(x, trip.pk)),
    )
    assert old == new == []


def test_parity_all_trips_done(clock):
    lead = _lead()
    _trip(lead, hours=-30, trip_status=Reservation.TripStatus.DONE)

    def old(x):
        touchpoints.schedule_post_trip(x)
        touchpoints.schedule_post_trip(x)

    def new(x):
        events.emit("all_trips_done", x)
        events.emit("all_trips_done", x)

    got_old, got_new = _parity(lead, old, new)
    assert got_old == got_new and len(got_new) == 2


def test_a_wedding_order_also_queues_the_wedding_thank_you(clock):
    """Departure: the old scheduler queued one `thank_you` and chose the wedding wording at
    send time; the wedding thank-you is its own row now, queued beside it, and the plain one
    stands down at send time (`rules`, "wedding thank-you sends instead")."""
    lead = _lead()
    _trip(lead, hours=-30, wedding=True, trip_status=Reservation.TripStatus.DONE)

    old, new = _parity(
        lead, touchpoints.schedule_post_trip, lambda x: events.emit("all_trips_done", x)
    )

    extra = ("thank_you_wedding", 0, NOW + timedelta(hours=2), S.SCHEDULED)
    assert sorted([*old, extra]) == new


def test_a_legacy_thank_you_sent_as_the_wedding_one_blocks_it(clock):
    lead = _lead()
    _trip(lead, hours=-30, wedding=True, trip_status=Reservation.TripStatus.DONE)
    TouchPointFactory(lead=lead, kind=K.THANK_YOU, message=_row("thank_you_wedding"), status=S.SENT)

    events.emit("all_trips_done", lead)

    assert not _tps(lead, kind="thank_you_wedding").exists()


@pytest.mark.parametrize("message", ["thank_you", None], ids=["seeded-pointer", "no-pointer"])
def test_an_order_the_old_engine_already_thanked_gets_no_wedding_thank_you(clock, message):
    """The old engine sent one thank-you, in the wedding wording for a wedding: an event
    repeated after the cut-over must not thank the couple twice."""
    lead = _lead()
    _trip(lead, hours=-30, wedding=True, trip_status=Reservation.TripStatus.DONE)
    TouchPointFactory(
        lead=lead,
        kind=K.THANK_YOU,
        message=_row(message) if message else None,
        status=S.SENT,
    )

    events.emit("all_trips_done", lead)

    assert not _tps(lead, kind="thank_you_wedding").exists()
    assert _tps(lead, kind=K.REVIEW_REQUEST).count() == 1


# --- parity: dates -----------------------------------------------------------------------


def test_parity_pickup_rows(clock):
    lead = _lead()
    _trip(lead)
    _trip(lead, hours=24, wedding=True)  # booked late: every anchor is in the past
    _trip(lead, source_leg_id="guests-in")
    _trip(lead, trip_status=Reservation.TripStatus.CANCELLED)
    ReservationFactory(lead=lead, pickup_date=None)

    def old(x):
        touchpoints.schedule_service_touchpoints(x)
        touchpoints.schedule_service_touchpoints(x)

    def new(x):
        events.schedule_dates(x)
        events.schedule_dates(x)

    got_old, got_new = _parity(lead, old, new)
    assert got_old == got_new
    assert {k for k, *_ in got_new} == {
        "trip_confirm_customer",
        "trip_confirm_customer_2",
        "wed_final_details",
    }


def test_a_cancelled_row_does_not_block_a_fresh_pickup_row(clock):
    lead = _lead()
    trip = _trip(lead)

    def play(schedule):
        def steps(x):
            schedule(x)
            _tps(x).filter(kind=K.TRIP_CONFIRM_CUSTOMER).update(status=S.CANCELLED)
            _tps(x).filter(kind=K.TRIP_CONFIRM_CUSTOMER_2).update(status=S.SENT)
            schedule(x)

        return steps

    old, new = _parity(
        lead, play(touchpoints.schedule_service_touchpoints), play(events.schedule_dates)
    )
    assert old == new
    assert (
        _run(lead, play(events.schedule_dates)).count(
            ("trip_confirm_customer", trip.pk, trip.pickup_at - timedelta(hours=72), S.SCHEDULED)
        )
        == 1
    )


def test_no_pickup_rows_before_the_order_books(clock):
    lead = _lead(Lead.Status.QUOTED)
    _trip(lead)
    events.schedule_dates(lead)
    assert not _tps(lead).exists()


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        {"channel": Assignment.Channel.GNET},
        {"in_house": True},
    ],
    ids=["manual", "gnet", "in-house"],
)
def test_parity_affiliate_confirmation(clock, coverage):
    lead = _lead()
    trip = _trip(lead)
    assignment = _farm_out(trip, **coverage)
    kinds = [K.TRIP_CONFIRM_AFFILIATE]

    old, new = _parity(
        lead,
        lambda x: touchpoints.schedule_affiliate_confirmation(
            Assignment.objects.get(pk=assignment.pk)
        ),
        lambda x: events.schedule_dates(x, _trip_of(x, trip.pk)),
        kinds,
    )

    assert old == new
    assert bool(new) == (coverage == {})


def test_booking_queues_the_affiliate_row_only_for_a_manual_farm_out(clock):
    """The seeded affiliate row keeps today's creation gate: no affiliate TouchPoint on a
    trip covered in-house, by GNet, or not yet covered."""
    lead = _lead()
    manual, gnet, in_house, open_ = _trip(lead), _trip(lead), _trip(lead), _trip(lead)
    _farm_out(manual)
    _farm_out(gnet, channel=Assignment.Channel.GNET)
    _farm_out(in_house, in_house=True)

    events.schedule_dates(lead)

    affiliate = _tps(lead, kind=K.TRIP_CONFIRM_AFFILIATE)
    assert list(affiliate.values_list("reservation", flat=True)) == [manual.pk]
    assert open_.pk


def test_an_added_affiliate_row_on_pickup_follows_the_same_gate(clock):
    row = _added(
        trigger="pickup",
        audience=MessageTemplate.Audience.AFFILIATE,
        recipients=["company"],
        timing_amount=24,
        timing_unit="hours",
        timing_direction="before",
    )
    lead = _lead()
    manual, in_house = _trip(lead), _trip(lead)
    _farm_out(manual)
    _farm_out(in_house, in_house=True)

    events.schedule_dates(lead)

    assert list(_tps(lead, message=row).values_list("reservation", flat=True)) == [manual.pk]


def _plan(lead, **kw):
    kw.setdefault("stripe_payment_method_id", "pm_1")
    kw.setdefault("balance_status", PaymentPlan.BalanceStatus.SCHEDULED)
    return PaymentPlanFactory(lead=lead, quote_total="1000.00", deposit_pct=50, **kw)


@pytest.mark.parametrize("case", ["card", "no-card", "on-account", "twice", "cancelled-blocks"])
def test_parity_balance_due(clock, case):
    lead = _lead()
    _trip(lead, hours=24 * 60)
    _plan(lead, **({"stripe_payment_method_id": ""} if case == "no-card" else {}))
    if case == "on-account":
        lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
        lead.save(update_fields=["billing_group"])
    kinds = [K.PAYMENT_REMINDER]

    def play(schedule):
        def steps(x):
            schedule(x)
            if case == "twice":
                schedule(x)
            if case == "cancelled-blocks":
                _tps(x).update(status=S.CANCELLED)
                schedule(x)

        return steps

    old, new = _parity(
        lead, play(touchpoints.schedule_payment_reminder), play(events.schedule_dates), kinds
    )
    assert old == new
    assert bool(new) == (case in ("card", "twice", "cancelled-blocks"))


@pytest.mark.parametrize(
    "balance", [PaymentPlan.BalanceStatus.NA, PaymentPlan.BalanceStatus.PAID], ids=str
)
def test_no_payment_reminder_unless_the_balance_is_scheduled(clock, balance):
    """Today's reminder is made when the balance becomes scheduled; a paid-in-full order
    with a card on file never gets a reminder queued (it would only be skipped)."""
    lead = _lead()
    _trip(lead, hours=24 * 60)
    _plan(lead, balance_status=balance)

    events.schedule_dates(lead)

    assert not _tps(lead, kind=K.PAYMENT_REMINDER).exists()


def test_an_order_date_row_matches_its_service_types(clock):
    row = _added(
        trigger="balance_due", timing_amount=1, timing_unit="days", timing_direction="before"
    )
    row.service_types.set([ServiceTypeFactory(name="Charter")])
    lead = _lead()
    _trip(lead, hours=24 * 60)
    _plan(lead)

    events.schedule_dates(lead)

    assert _tps(lead, kind=K.PAYMENT_REMINDER).exists()
    assert not _tps(lead, message=row).exists()


def test_parity_quote_expiry_rows_follow_the_quote_not_the_booking(clock):
    """A booked order's schedule_dates never revives the quote's expiry nudges."""
    lead = _lead(quote_expires_at=NOW + timedelta(days=3))
    _trip(lead)
    events.schedule_dates(lead)
    assert not _tps(lead, kind__in=[K.TP7_EXPIRING, K.TP8_EXPIRED]).exists()


# --- parity: a moved pickup --------------------------------------------------------------


def _confirmed_trip(lead, **coverage):
    trip = _trip(lead, customer_confirmed_at=NOW - timedelta(hours=1))
    assignment = _farm_out(trip, affiliate_confirmed_at=NOW - timedelta(hours=1), **coverage)
    return trip, assignment


def _moved(x, pk, schedule):
    trip = _trip_of(x, pk)
    trip.pickup_date = trip.pickup_date + timedelta(days=2)
    trip.save()
    schedule(trip)
    trip.refresh_from_db()
    assignment = trip.assignments.get()
    assert trip.customer_confirmed_at is None
    assert assignment.affiliate_confirmed_at is None


def test_parity_reschedule_trip(clock):
    lead = _lead()
    trip, assignment = _confirmed_trip(lead)
    other = _trip(lead)
    touchpoints.schedule_service_touchpoints(lead)
    touchpoints.schedule_affiliate_confirmation(assignment)
    _tps(lead, reservation=trip, kind=K.TRIP_CONFIRM_CUSTOMER).update(status=S.SENT)

    old, new = _parity(
        lead,
        lambda x: _moved(x, trip.pk, touchpoints.reschedule_service_touchpoints),
        lambda x: _moved(x, trip.pk, events.reschedule_trip),
    )

    assert old == new
    moved_pickup = _trip_of(lead, trip.pk).pickup_at + timedelta(days=2)
    assert ("trip_confirm_customer", trip.pk, moved_pickup - timedelta(hours=72), S.SCHEDULED) in (
        new
    )
    assert ("trip_confirm_affiliate", trip.pk, moved_pickup - timedelta(hours=48), S.SCHEDULED) in (
        new
    )
    assert all(status == S.SCHEDULED for _, pk, _, status in new if pk == other.pk)


def test_a_moved_in_house_trip_gets_no_affiliate_row(clock):
    """Departure: the old reschedule queued an affiliate confirmation for any confirmed
    coverage, which the sender then skipped for in-house and GNet trips; the creation gate
    now holds here too."""
    lead = _lead()
    trip, _ = _confirmed_trip(lead, in_house=True)
    touchpoints.schedule_service_touchpoints(lead)
    kinds = [K.TRIP_CONFIRM_AFFILIATE]

    old, new = _parity(
        lead,
        lambda x: _moved(x, trip.pk, touchpoints.reschedule_service_touchpoints),
        lambda x: _moved(x, trip.pk, events.reschedule_trip),
        kinds,
    )

    assert len(old) == 1 and new == []


def test_pickup_moved_gets_a_fresh_confirmation(clock):
    lead = _lead()
    trip = _trip(lead)
    events.schedule_dates(lead)
    sent = _tps(lead, kind=K.TRIP_CONFIRM_CUSTOMER).get()
    TouchPoint.objects.filter(pk=sent.pk).update(status=S.SENT)

    trip.pickup_date += timedelta(days=1)
    trip.save()
    events.reschedule_trip(trip)

    sent.refresh_from_db()
    fresh = _tps(lead, kind=K.TRIP_CONFIRM_CUSTOMER, status=S.SCHEDULED).get()
    assert sent.status == S.CANCELLED
    assert fresh.scheduled_for == trip.pickup_at - timedelta(hours=72)
    assert fresh.message == _row("trip_confirm_customer")


def test_a_moved_pickup_leaves_the_driver_message_to_sync_waiting(clock):
    lead = _lead()
    trip = _trip(lead, hours=72)
    _dispatched(trip)
    events.sync_waiting(trip)
    driver = _tps(lead, kind=K.DRIVER_RELEASED).get()

    events.reschedule_trip(trip)

    driver.refresh_from_db()
    assert driver.status == S.SCHEDULED


# --- parity: rows that wait ------------------------------------------------------------


def _old_sync(pk):
    return lambda x: touchpoints.sync_driver_message(_trip_of(x, pk))


def _new_sync(pk):
    return lambda x: events.sync_waiting(_trip_of(x, pk))


@pytest.mark.parametrize("hours", [72, 3], ids=["dispatched-early", "dispatched-late"])
def test_parity_driver_message(clock, hours):
    lead = _lead()
    trip = _trip(lead, hours=hours)
    _dispatched(trip)

    def twice(sync):
        def steps(x):
            sync(x)
            sync(x)

        return steps

    old, new = _parity(lead, twice(_old_sync(trip.pk)), twice(_new_sync(trip.pk)))

    assert old == new
    assert new == [
        ("driver_released", trip.pk, max(NOW, trip.pickup_at - timedelta(hours=12)), S.SCHEDULED)
    ]


@pytest.mark.parametrize(
    "state",
    ["not-dispatched", "no-driver", "pickup-passed", "switched-off", "in-house"],
)
def test_parity_driver_message_gates(clock, state):
    lead = _lead()
    trip = _trip(lead, hours=-1 if state == "pickup-passed" else 72)
    if state == "not-dispatched":
        _farm_out(trip, **DRIVER)
    elif state == "no-driver":
        _dispatched(trip, driver_name="", driver_cell="")
    elif state == "in-house":
        _farm_out(trip, in_house=True)
        trip.trip_status = Reservation.TripStatus.DISPATCHED
        trip.save()
    else:
        _dispatched(trip)
    if state == "switched-off":
        MessageTemplate.objects.filter(key="driver_released").update(enabled=False)

    old, new = _parity(lead, _old_sync(trip.pk), _new_sync(trip.pk))

    assert old == new
    assert bool(new) == (state == "in-house")


def test_parity_a_held_driver_message_follows_the_pickup(clock):
    lead = _lead()
    trip = _trip(lead, hours=72)
    _dispatched(trip)
    touchpoints.sync_driver_message(trip)
    trip.trip_status = Reservation.TripStatus.ASSIGNED  # dropped below Dispatched: held
    trip.pickup_date += timedelta(days=1)
    trip.save()

    old, new = _parity(lead, _old_sync(trip.pk), _new_sync(trip.pk))

    assert old == new
    assert new[0][2] == trip.pickup_at - timedelta(hours=12)


def _sent_driver_message(lead, trip, details):
    TouchPointFactory(
        lead=lead,
        reservation=trip,
        kind=K.DRIVER_RELEASED,
        message=_row("driver_released"),
        status=S.SENT,
        sent_at=NOW - timedelta(hours=1),
        driver_details=details,
    )


@pytest.mark.parametrize(
    "details",
    [
        {"name": "Old Driver", "cell": "+15715550000", "vehicle_desc": "", "vehicle_number": ""},
        None,
        "same",
    ],
    ids=["changed", "unknown", "unchanged"],
)
def test_parity_driver_changed(clock, details):
    lead = _lead()
    trip = _trip(lead, hours=6)
    assignment = _dispatched(trip)
    if details == "same":
        details = assignment.driver_info
    _sent_driver_message(lead, trip, details)

    def twice(sync):
        def steps(x):
            sync(x)
            sync(x)

        return steps

    old, new = _parity(
        lead, twice(_old_sync(trip.pk)), twice(_new_sync(trip.pk)), [K.DRIVER_UPDATED]
    )

    assert old == new
    assert len(new) == (1 if details and details != assignment.driver_info else 0)


def test_a_status_change_costs_no_message_row_queries():
    """As the old `sync_driver_message`: a non-dispatch status with nothing queued reads
    neither the message rows nor the master switch."""
    lead = _lead()
    trip = _trip(lead)
    _farm_out(trip, **DRIVER)
    trip.trip_status = Reservation.TripStatus.ASSIGNED
    trip.save()

    with CaptureQueriesContext(connection) as ctx:
        assert events.sync_waiting(trip) is None

    sql = " ".join(q["sql"] for q in ctx.captured_queries)
    assert "messaging_messagetemplate" not in sql
    assert "messaging_notificationconfig" not in sql


# --- catch-up (spec section 5) -------------------------------------------------------------


def test_booked_three_hours_out_then_dispatched_now_sends_at_once(
    live, django_capture_on_commit_callbacks
):
    lead = _lead()
    trip = _trip(lead, hours=3)
    _dispatched(trip)

    with patch(SEND, side_effect=_ok) as send, django_capture_on_commit_callbacks(execute=True):
        events.sync_waiting(trip)

    tp = _tps(lead, kind=K.DRIVER_RELEASED).get()
    assert tp.status == S.SENT and tp.message == _row("driver_released")
    assert tp.driver_details["name"] == "Sam Rivera"
    assert send.called


def test_a_held_row_that_becomes_ready_sends_on_commit(live, django_capture_on_commit_callbacks):
    lead = _lead()
    trip = _trip(lead, hours=3)
    assignment = _dispatched(trip, driver_cell="")
    TouchPointFactory(
        lead=lead,
        reservation=trip,
        kind=K.DRIVER_RELEASED,
        message=_row("driver_released"),
        scheduled_for=timezone.now() - timedelta(hours=1),
    )
    assignment.driver_cell = DRIVER["driver_cell"]
    assignment.save()

    with patch(SEND, side_effect=_ok), django_capture_on_commit_callbacks(execute=True):
        events.sync_waiting(trip)

    assert _tps(lead, kind=K.DRIVER_RELEASED).get().status == S.SENT


def test_a_driver_change_after_the_message_sends_the_update_on_commit(
    live, django_capture_on_commit_callbacks
):
    lead = _lead()
    trip = _trip(lead, hours=6)
    _dispatched(trip)
    _sent_driver_message(lead, trip, {"name": "Old Driver", "cell": "+15715550000"})

    with patch(SEND, side_effect=_ok), django_capture_on_commit_callbacks(execute=True):
        events.sync_waiting(trip)

    assert _tps(lead, kind=K.DRIVER_UPDATED).get().status == S.SENT


def test_a_date_row_made_late_sends_on_commit(live, django_capture_on_commit_callbacks):
    """Booked a day out: the 72-hour confirmation's time has passed, so it goes now."""
    lead = _lead()
    trip = _trip(lead, hours=24)

    with patch(SEND, side_effect=_ok) as send, django_capture_on_commit_callbacks(execute=True):
        events.schedule_dates(lead)

    tp = _tps(lead, kind=K.TRIP_CONFIRM_CUSTOMER).get()
    assert tp.status == S.SENT and tp.reservation == trip
    assert send.called


def test_a_moved_pickup_inside_the_window_confirms_on_commit(
    live, django_capture_on_commit_callbacks
):
    lead = _lead()
    trip = _trip(lead)
    events.schedule_dates(lead)
    trip.pickup_date = (timezone.now() + timedelta(days=1)).astimezone(EASTERN).date()
    trip.save()

    with patch(SEND, side_effect=_ok), django_capture_on_commit_callbacks(execute=True):
        events.reschedule_trip(trip)

    assert _tps(lead, kind=K.TRIP_CONFIRM_CUSTOMER, status=S.SENT).count() == 1


def test_a_row_not_yet_due_waits_for_the_cron(live, django_capture_on_commit_callbacks):
    lead = _lead()
    _trip(lead)

    with django_capture_on_commit_callbacks() as callbacks:
        events.schedule_dates(lead)

    assert callbacks == []


# --- cancel --------------------------------------------------------------------------------


def _every_kind(lead, trip):
    for kind in [*K.values, "thank_you_wedding"]:
        TouchPointFactory(
            lead=lead,
            reservation=trip if kind in touchpoints._RESERVATION_KINDS else None,
            kind=kind,
            scheduled_for=NOW + timedelta(days=1),
        )
    TouchPointFactory(lead=lead, kind=K.TP1_WELCOME, status=S.SENT)


@pytest.mark.parametrize("sweep", ["default", "everything"])
def test_parity_cancel(clock, sweep):
    lead = _lead()
    trip = _trip(lead)
    _every_kind(lead, trip)
    kinds = None if sweep == "default" else list(K.values)
    triggers = None if sweep == "default" else events.EVERY

    old, new = _parity(
        lead,
        lambda x: touchpoints.cancel_pending(x, kinds=kinds),
        lambda x: events.cancel(x, triggers=triggers),
        list(K.values),
    )
    assert old == new


def test_the_default_cancel_spares_the_new_payment_events(clock):
    row = _added(trigger="deposit_paid", timing_amount=1, timing_unit="hours")
    lead = _lead()
    events.emit("deposit_paid", lead)

    assert events.cancel(lead) == 0
    assert _tps(lead, message=row).get().status == S.SCHEDULED


def test_parity_cancel_the_affiliate_confirmation(clock):
    lead = _lead()
    trip, other = _trip(lead), _trip(lead)
    for t in (trip, other):
        _farm_out(t)
    events.schedule_dates(lead)

    old, new = _parity(
        lead,
        lambda x: touchpoints.cancel_affiliate_confirmation(_trip_of(x, trip.pk)),
        lambda x: events.cancel(
            x,
            triggers=["pickup"],
            reservation=_trip_of(x, trip.pk),
            audience=MessageTemplate.Audience.AFFILIATE,
        ),
    )
    assert old == new
    withdrawn = ("trip_confirm_affiliate", trip.pk, trip.pickup_at - timedelta(hours=48))
    assert (*withdrawn, S.CANCELLED) in new


def test_cancel_counts_what_it_withdrew(clock):
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW, quote_expires_at=NOW + timedelta(days=3))
    events.emit("quote_sent", lead)
    assert events.cancel(lead) == 4


# --- redate ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,timing",
    [
        ("trip_confirm_customer", (96, "hours")),
        ("driver_released", (6, "hours")),
        ("driver_released", (20, "hours")),
        ("tp6_quote_followup", (2, "days")),
        ("payment_reminder", (5, "days")),
    ],
)
def test_parity_redate(clock, key, timing):
    lead = _lead(quote_sent_at=NOW)
    trip = _trip(lead, hours=18)
    _dispatched(trip)
    _plan(lead)
    old_offset = _row(key).offset

    def setup(x):
        touchpoints.schedule_service_touchpoints(x)
        touchpoints.sync_driver_message(_trip_of(x, trip.pk))
        touchpoints.schedule_quote_sent(x)
        touchpoints.schedule_payment_reminder(x)
        MessageTemplate.objects.filter(key=key).update(
            timing_amount=timing[0], timing_unit=timing[1]
        )

    def old(x):
        setup(x)
        overrides.redate(key, from_offset=old_offset)

    def new(x):
        setup(x)
        events.redate(_row(key), old_offset=old_offset)

    got_old, got_new = _parity(lead, old, new)
    assert got_old == got_new


def test_redate_moves_rows_the_events_queued(clock):
    lead = _lead()
    trip = _trip(lead)
    events.schedule_dates(lead)
    row = _row("trip_confirm_customer")
    old_offset = row.offset
    row.timing_amount = 96
    row.save()

    assert events.redate(row, old_offset=old_offset) == 1
    assert _tps(lead, message=row).get().scheduled_for == trip.pickup_at - timedelta(hours=96)


def test_redate_of_a_staff_click_is_nothing():
    row = _added(trigger="send_quote")
    assert events.redate(row, old_offset=timedelta(0)) == 0


# --- the claim ---------------------------------------------------------------------------


def test_a_row_redated_in_bulk_after_it_was_read_is_not_sent_early(live):
    lead = _lead()
    row = _added(trigger="deposit_paid")
    tp = TouchPointFactory(
        lead=lead, kind=row.key, message=row, scheduled_for=timezone.now() - timedelta(minutes=1)
    )
    read = TouchPoint.objects.get(pk=tp.pk)
    tp.scheduled_for = timezone.now() + timedelta(days=1)
    TouchPoint.objects.bulk_update([tp], ["scheduled_for"])  # leaves updated_at as it was

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(read) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == S.SCHEDULED


# --- fix round 1 ---------------------------------------------------------------------------


def test_the_seeded_dispatched_status_message_is_retired(clock):
    """It is never queued while it stays on its own trigger, exactly as today; a message an
    Admin adds on that trigger is."""
    _switch_on("status_dispatched")
    added = _added(trigger="status_dispatched")
    lead = _lead()
    trip = _trip(lead)

    made = events.emit("status_dispatched", lead, trip)

    assert [tp.message for tp in made] == [added]
    assert not _tps(lead, kind=K.STATUS_DISPATCHED).exists()


def test_the_seeded_dispatched_message_moved_to_another_trigger_queues_there(clock):
    MessageTemplate.objects.filter(key="status_dispatched").update(
        trigger="status_on_the_way", enabled=True
    )
    lead = _lead()
    trip = _trip(lead)

    events.emit("status_on_the_way", lead, trip)

    assert _tps(lead, kind=K.STATUS_DISPATCHED, reservation=trip).count() == 1


def test_the_retired_dispatched_message_says_so_in_the_editor():
    from apps.messaging import rules

    assert any("Retired" in line for line in rules.built_in_descriptions("status_dispatched"))


def test_parity_quote_resent_with_the_master_switch_off(clock):
    """A re-sent quote withdraws the old program even while nothing new may be queued."""
    lead = _lead(Lead.Status.QUOTED, quote_sent_at=NOW, quote_expires_at=NOW + timedelta(days=3))
    touchpoints.schedule_quote_sent(lead)
    touchpoints.schedule_quote_viewed(lead)
    NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})

    old, new = _parity(lead, touchpoints.schedule_quote_sent, _quote_sent_new)

    assert old == new
    assert {status for *_, status in new} == {S.CANCELLED}


@pytest.mark.parametrize(
    "message", [None, "thank_you"], ids=["legacy-no-pointer", "seeded-pointer"]
)
def test_a_thank_you_still_queued_from_before_the_cut_over_blocks_the_wedding_one(clock, message):
    """At the cut-over a wedding order may hold the old engine's queued `thank_you`, which
    the sender will send in the wedding words: a repeated event must not add a second."""
    lead = _lead()
    _trip(lead, hours=-30, wedding=True, trip_status=Reservation.TripStatus.DONE)
    TouchPointFactory(
        lead=lead,
        kind=K.THANK_YOU,
        message=_row(message) if message else None,
        scheduled_for=NOW + timedelta(hours=1),
    )

    events.emit("all_trips_done", lead)

    assert not _tps(lead, kind="thank_you_wedding").exists()
    assert _tps(lead, kind=K.THANK_YOU).count() == 1


def test_send_due_never_raises(live, caplog):
    with patch.object(sender, "_send_batch", side_effect=RuntimeError("boom")):
        sender.send_due([1, 2])

    assert "boom" in caplog.text


def test_a_failing_on_commit_send_never_reaches_the_caller(
    live, django_capture_on_commit_callbacks
):
    _added(trigger="deposit_paid")
    lead = _lead()

    with (
        patch.object(sender, "send_due", side_effect=RuntimeError("boom")) as send_due,
        django_capture_on_commit_callbacks(execute=True),
    ):
        (tp,) = events.emit("deposit_paid", lead)

    send_due.assert_called_once_with([tp.pk])
