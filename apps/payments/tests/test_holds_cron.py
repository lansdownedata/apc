"""APC-95 — the hourly `cleaning-holds` job and the cancellation release (spec 2026-10-27 §4).

The job places each held order's hold once the placement time has come in its first held
trip's own zone, and sweeps: releases holds left unused N days after the last held trip,
marks ones past the capture window expired, flags a capture that was never booked, and
settles holds Stripe never answered for. Cancelling every held trip releases the hold.
Stripe is mocked throughout and the key is blanked so an unmocked call can't reach it.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import stripe
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department, User
from apps.core import cron
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import holds, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview
from apps.tasks.models import Task, TaskConfig

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 11, 20)
_types = count()


@pytest.fixture(autouse=True)
def _no_real_stripe(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


@pytest.fixture(autouse=True)
def _holds_on():
    config = TaskConfig.load()
    config.hold_enabled = True
    config.save()


def _at(day: date, clock: time, zone: ZoneInfo = NY) -> datetime:
    """A wall-clock moment in `zone` (never UTC arithmetic: DST days have 23 or 25 hours)."""
    return datetime.combine(day, clock, tzinfo=zone)


def _order(*fees, day=DAY, zone="America/New_York", card=True):
    """A booked card order with one trip per fee (each its own vehicle type), a day apart
    starting on `day`."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trips = []
    for i, fee in enumerate(fees or ("500.00",)):
        vehicle = VehicleTypeFactory(name=f"Cron type {next(_types)}", cleaning_fee=Decimal(fee))
        trips.append(
            ReservationFactory(
                lead=lead,
                vehicle=vehicle,
                pickup_date=day + timedelta(days=i),
                pickup_time=time(9, 0),
                pickup_timezone=zone,
            )
        )
    lead.refresh_from_db()
    plan = PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    if card:
        plan.stripe_customer_id = "cus_1"
        plan.stripe_payment_method_id = "pm_1"
        plan.card_brand, plan.card_last4 = "visa", "4242"
        plan.save()
    return lead, plan, trips


def _intent(pi_id="pi_hold", *, status="requires_capture", amount=50000):
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        amount_received=0,
        latest_charge="ch_1",
        metadata={"kind": "hold"},
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _run(now):
    """One run of the job at `now`, Stripe answering every create with a fresh hold."""
    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as create:
        processed = holds.run_cron(now=now)
    return processed, create


def _placed(*fees, day=DAY):
    lead, plan, trips = _order(*fees, day=day)
    _run(_at(day, time(3, 0)))
    hold = holds.current_hold(lead)
    assert hold is not None and hold.status == Charge.Status.AUTHORIZED
    return lead, plan, trips, hold


def _hold_rows(lead):
    return Charge.objects.filter(plan__lead=lead, kind=Charge.Kind.HOLD)


# --- placing --------------------------------------------------------------------------------


def test_nothing_before_place_time_one_hold_after():
    lead, _plan, _trips = _order("500.00")

    processed, create = _run(_at(DAY, time(2, 59)))
    assert (processed, create.call_count) == (0, 0)
    assert not _hold_rows(lead).exists()

    processed, create = _run(_at(DAY, time(3, 0)))
    assert (processed, create.call_count) == (1, 1)
    hold = _hold_rows(lead).get()
    assert (hold.status, hold.amount) == (Charge.Status.AUTHORIZED, Decimal("500.00"))


def test_two_runs_one_row():
    lead, _plan, _trips = _order("500.00")

    _first, create_first = _run(_at(DAY, time(4, 0)))
    second, create_second = _run(_at(DAY, time(5, 0)))

    assert create_first.call_count == 1
    assert (second, create_second.call_count) == (0, 0)
    assert _hold_rows(lead).count() == 1


def test_placement_follows_the_trips_zone():
    lead, _plan, _trips = _order("500.00", zone="America/Los_Angeles")

    # 09:00 UTC is 1 AM in Los Angeles, but already 4 AM in New York (the business zone):
    # placing now would be reading the wrong clock.
    processed, create = _run(datetime.combine(DAY, time(9, 0), tzinfo=UTC))
    assert (processed, create.call_count) == (0, 0)

    # 11:00 UTC is 3 AM in Los Angeles.
    processed, create = _run(datetime.combine(DAY, time(11, 0), tzinfo=UTC))
    assert (processed, create.call_count) == (1, 1)
    assert _hold_rows(lead).get().status == Charge.Status.AUTHORIZED


def test_dst_change_day_is_covered():
    # 2026-11-01: New York falls back at 2 AM, so the day is 25 hours long.
    fall_back = date(2026, 11, 1)
    lead, _plan, _trips = _order("500.00", day=fall_back)

    processed, _create = _run(_at(fall_back, time(2, 59)))
    assert processed == 0

    processed, create = _run(_at(fall_back, time(3, 30)))
    assert (processed, create.call_count) == (1, 1)
    assert _hold_rows(lead).get().status == Charge.Status.AUTHORIZED


def _switch_off():
    config = TaskConfig.load()
    config.hold_enabled = False
    config.save()


def test_switch_off_places_nothing_but_still_sweeps():
    # A live hold placed while the switch was on is never orphaned by turning it off.
    held, _plan, (trip,), hold = _placed("500.00")
    trip.trip_status = Reservation.TripStatus.CANCELLED
    trip.save(update_fields=["trip_status"])
    _switch_off()
    lead, _plan2, _trips2 = _order("500.00")

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        processed, create = _run(_at(DAY, time(9, 0)))

    assert (processed, create.call_count) == (1, 0)
    assert not _hold_rows(lead).exists()
    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED


def test_switch_off_drops_a_pending_hold_instead_of_resending_it():
    lead, _plan, _trips = _order("500.00")
    with patch.object(
        stripe.PaymentIntent, "create", side_effect=stripe.error.APIConnectionError("no route")
    ):
        hold = holds.place_hold(lead, now=_at(DAY, time(3, 0)))
    assert hold.status == Charge.Status.PENDING
    _switch_off()

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        processed, create = _run(_at(DAY, time(4, 0)))

    assert (processed, create.call_count) == (1, 0)
    cancel.assert_not_called()  # no intent id: nothing at Stripe to cancel
    hold.refresh_from_db()
    assert (hold.status, hold.note) == (Charge.Status.RELEASED, "holds switched off")
    assert not Task.objects.filter(lead=lead, kind=holds.TASK_KIND).exists()


def test_order_already_over_is_not_held():
    # One trip on the 1st: from the 2nd on it is behind us, in any run.
    over, _plan, _trips = _order("500.00", day=date(2026, 11, 1))
    for day in (date(2026, 11, 2), date(2026, 11, 4), date(2026, 11, 5)):
        assert holds.due_for_placement(_at(day, time(10, 0))) == []

    processed, create = _run(_at(date(2026, 11, 2), time(10, 0)))
    assert (processed, create.call_count) == (0, 0)
    assert not _hold_rows(over).exists()


def test_a_past_dated_order_is_not_held_at_go_live():
    # Switched on mid-order: the first held trip was yesterday, the next is tomorrow.
    midway, _plan, _trips = _order("500.00", "500.00", "500.00", day=DAY - timedelta(days=1))
    # A same-day order booked after the placement time still is.
    late, _plan2, _trips2 = _order("500.00")

    assert holds.due_for_placement(_at(DAY, time(14, 0))) == [late]


def test_an_order_whose_held_trips_are_all_processed_is_not_held():
    lead, _plan, (trip,) = _order("500.00")
    TripReview.objects.update_or_create(
        reservation=trip, defaults={"processed_at": _at(DAY, time(1, 0))}
    )
    assert holds.due_for_placement(_at(DAY, time(4, 0))) == []


def test_sweep_releases_when_every_held_trip_is_processed():
    lead, _plan, (first, second), hold = _placed("500.00", "250.00")
    TripReview.objects.update_or_create(
        reservation=first, defaults={"processed_at": _at(DAY, time(20, 0))}
    )

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(_at(DAY, time(21, 0))) == 0
        TripReview.objects.update_or_create(
            reservation=second, defaults={"processed_at": _at(DAY + timedelta(days=1), time(20, 0))}
        )
        assert holds.sweep(_at(DAY + timedelta(days=1), time(21, 0))) == 1

    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert (hold.status, hold.note) == (Charge.Status.RELEASED, "all held trips processed")


def test_due_for_placement_skips_unheld_and_already_held_orders():
    held, _plan, _trips = _order("500.00")
    _order("0.00")  # no cleaning fee
    _order("500.00", card=True)[0].reservations.update(trip_status=Reservation.TripStatus.CANCELLED)
    already, _plan2, _trips2 = _order("500.00")
    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()):
        holds.place_hold(already)

    assert holds.due_for_placement(_at(DAY, time(3, 0))) == [held]


def test_due_for_placement_is_a_fixed_number_of_queries(django_assert_max_num_queries):
    for _ in range(4):
        _order("500.00", "250.00")
    TaskConfig.load()
    with django_assert_max_num_queries(6):
        due = holds.due_for_placement(_at(DAY, time(3, 0)))
    assert len(due) == 4
    # One row per order from the database, whatever plan it picks (2 held trips each).
    rows = list(holds._candidates(_at(DAY, time(3, 0))))
    assert len(rows) == len({lead.pk for lead in rows}) == 4


# --- the sweep ------------------------------------------------------------------------------


def test_sweep_releases_after_n_days():
    lead, _plan, (trip,), hold = _placed("500.00")
    hold.capture_expires_at = _at(DAY + timedelta(days=30), time(0, 0))
    hold.save(update_fields=["capture_expires_at"])

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(_at(DAY + timedelta(days=3), time(23, 59))) == 0
        cancel.assert_not_called()
        assert holds.sweep(_at(DAY + timedelta(days=4), time(0, 30))) == 1

    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED
    assert hold.note == "unused after the last held trip"


def test_sweep_releases_a_hold_with_no_held_trip_left():
    lead, _plan, (trip,), hold = _placed("500.00")
    trip.trip_status = Reservation.TripStatus.CANCELLED
    trip.save(update_fields=["trip_status"])

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(_at(DAY, time(12, 0))) == 1

    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED


def test_sweep_marks_past_capture_window_expired():
    lead, _plan, _trips, hold = _placed("500.00")
    now = _at(DAY, time(12, 0))
    hold.capture_expires_at = now - timedelta(minutes=1)
    hold.save(update_fields=["capture_expires_at"])

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(now) == 1

    cancel.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED


def test_sweep_flags_a_captured_unbooked_hold_once():
    lead, _plan, _trips, hold = _placed("500.00")
    # Captured at Stripe (the webhook saw it), never booked to a trip; long past every date.
    hold.captured_at = _at(DAY, time(10, 0))
    hold.capture_expires_at = _at(DAY, time(11, 0))
    hold.save(update_fields=["captured_at", "capture_expires_at"])
    later = _at(DAY + timedelta(days=20), time(12, 0))

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(later) == 1
        assert holds.sweep(later + timedelta(hours=1)) == 0

    cancel.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED
    task = Task.objects.get(lead=lead, kind=holds.UNBOOKED_TASK_KIND)
    assert task.department == Department.ACCOUNTING
    assert task.status == Task.Status.OPEN
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.HOLD_UNBOOKED).count() == 1


def test_sweep_fails_a_pending_hold_stripe_never_answered():
    lead, _plan, _trips = _order("500.00")
    outage = stripe.error.APIConnectionError("no route")
    with patch.object(stripe.PaymentIntent, "create", side_effect=outage):
        hold = holds.place_hold(lead, now=_at(DAY, time(3, 0)))
    assert hold.status == Charge.Status.PENDING
    old = _at(DAY, time(3, 0)) - timedelta(days=services.AUTH_HOLD_DAYS + 1)
    Charge.objects.filter(pk=hold.pk).update(created_at=old)

    with patch.object(stripe.PaymentIntent, "create", side_effect=outage) as create:
        assert holds.sweep(_at(DAY, time(12, 0))) == 1

    assert create.call_count == 1  # one last try first
    hold.refresh_from_db()
    assert hold.status == Charge.Status.FAILED
    task = Task.objects.get(lead=lead, kind=holds.TASK_KIND)
    assert task.note == "Cleaning hold could not be placed: no answer from Stripe"


def test_sweep_finishes_a_young_pending_hold():
    lead, _plan, _trips = _order("500.00")
    with patch.object(
        stripe.PaymentIntent, "create", side_effect=stripe.error.APIConnectionError("no route")
    ):
        hold = holds.place_hold(lead, now=_at(DAY, time(3, 0)))

    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as create:
        assert holds.sweep(_at(DAY, time(4, 0))) == 1

    assert create.call_args.kwargs["idempotency_key"] == hold.idempotency_key
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


def test_sweep_cancels_a_pending_intent_with_no_held_trip_left():
    lead, _plan, (trip,) = _order("500.00")
    with patch.object(stripe.PaymentIntent, "create", return_value=_intent(status="processing")):
        hold = holds.place_hold(lead, now=_at(DAY, time(3, 0)))
    assert (hold.status, hold.stripe_payment_intent_id) == (Charge.Status.PENDING, "pi_hold")
    trip.trip_status = Reservation.TripStatus.CANCELLED
    trip.save(update_fields=["trip_status"])

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        assert holds.sweep(_at(DAY, time(12, 0))) == 1

    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED


def test_sweep_cancels_a_pending_intent_only_while_it_is_still_pending():
    lead, _plan, (trip,) = _order("500.00")
    with patch.object(stripe.PaymentIntent, "create", return_value=_intent(status="processing")):
        stale = holds.place_hold(lead, now=_at(DAY, time(3, 0)))
    # An overlapping run settled the row after this one read it.
    Charge.objects.filter(pk=stale.pk).update(status=Charge.Status.AUTHORIZED)

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        settled = holds._settle_pending(
            lead, stale, _at(DAY, time(12, 0)), needed=False, enabled=True
        )
    assert settled == 0

    cancel.assert_not_called()
    stale.refresh_from_db()
    assert stale.status == Charge.Status.AUTHORIZED


@pytest.mark.parametrize("change", ["lost", "trips_cancelled"])
def test_sweep_quietly_settles_a_pending_hold_with_no_intent_nobody_needs(change):
    lead, _plan, (trip,) = _order("500.00")
    with patch.object(
        stripe.PaymentIntent, "create", side_effect=stripe.error.APIConnectionError("no route")
    ):
        hold = holds.place_hold(lead, now=_at(DAY, time(3, 0)))
    assert (hold.status, hold.stripe_payment_intent_id) == (Charge.Status.PENDING, "")
    if change == "lost":
        lead.status = Lead.Status.LOST
        lead.save(update_fields=["status"])
    else:
        trip.trip_status = Reservation.TripStatus.CANCELLED
        trip.save(update_fields=["trip_status"])
    old = _at(DAY, time(3, 0)) - timedelta(days=services.AUTH_HOLD_DAYS + 1)
    Charge.objects.filter(pk=hold.pk).update(created_at=old)

    with (
        patch.object(stripe.PaymentIntent, "create") as create,
        patch.object(stripe.PaymentIntent, "cancel") as cancel,
    ):
        assert holds.sweep(_at(DAY, time(12, 0))) == 1

    create.assert_not_called()
    cancel.assert_not_called()
    hold.refresh_from_db()
    assert (hold.status, hold.note) == (Charge.Status.RELEASED, "no held trip left")
    assert not Task.objects.filter(lead=lead).filter(kind__startswith="cleaning_hold").exists()
    assert not Notification.objects.filter(lead=lead).exists()
    lead.refresh_from_db()
    assert lead.has_alert is False


# --- cancelling -----------------------------------------------------------------------------


def _admin():
    return UserFactory(role=User.Role.OWNER_ADMIN)


def _soon() -> date:
    """A trip day ahead of the real clock: cancelling a past pickup takes other rules."""
    return timezone.localdate() + timedelta(days=10)


def test_cancelling_every_held_trip_releases_the_hold():
    lead, _plan, (van, _sedan), hold = _placed("500.00", "0.00", day=_soon())

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        services.cancel_trips(lead, [van], user=_admin())

    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert (hold.status, hold.note) == (Charge.Status.RELEASED, "order cancelled")


def test_cancelling_leaves_an_expired_hold_alone():
    lead, _plan, (van, _sedan), hold = _placed("500.00", "0.00", day=_soon())
    holds.mark_expired(hold)

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        services.cancel_trips(lead, [van], user=_admin())

    cancel.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED


def test_cancelling_one_of_two_held_trips_keeps_it():
    lead, _plan, (first, _second), hold = _placed("500.00", "250.00", day=_soon())

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        services.cancel_trips(lead, [first], user=_admin())

    cancel.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


def test_cancel_order_releases_the_hold():
    lead, plan, _trips, hold = _placed("500.00")
    plan.charges.create(
        kind=Charge.Kind.DEPOSIT,
        amount=Decimal("100.00"),
        status=Charge.Status.AUTHORIZED,
        stripe_payment_intent_id="pi_dep",
        idempotency_key=f"seed-dep-{plan.pk}",
    )

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        services.cancel_order(lead, reason="No car")

    assert sorted(c.args[0] for c in cancel.call_args_list) == ["pi_dep", "pi_hold"]
    hold.refresh_from_db()
    assert (hold.status, hold.note) == (Charge.Status.RELEASED, "order cancelled")


def test_a_release_stripe_refuses_never_undoes_a_cancel():
    lead, _plan, (van,), hold = _placed("500.00", day=_soon())
    err = stripe.error.APIConnectionError("down")

    with (
        patch.object(stripe.PaymentIntent, "cancel", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=err),
    ):
        services.cancel_trips(lead, [van], user=_admin())

    van.refresh_from_db()
    assert van.trip_status == Reservation.TripStatus.CANCELLED
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED  # the sweep lets it go next hour


# --- the job --------------------------------------------------------------------------------


def test_one_bad_order_does_not_stop_the_run():
    bad, _p1, _t1 = _order("500.00")
    good, _p2, _t2 = _order("500.00")
    answers = iter([RuntimeError("boom"), _intent(pi_id="pi_good")])

    def create(**kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with patch.object(stripe.PaymentIntent, "create", side_effect=create):
        processed = holds.run_cron(now=_at(DAY, time(3, 0)))

    assert processed == 1
    statuses = sorted(
        Charge.objects.filter(kind=Charge.Kind.HOLD, plan__lead__in=[bad, good]).values_list(
            "status", flat=True
        )
    )
    assert statuses == [Charge.Status.AUTHORIZED, Charge.Status.PENDING]


def test_cron_endpoint_is_registered(client, settings):
    settings.CRON_SECRET = "s3cret"
    assert cron.JOBS["cleaning-holds"] == holds.run_cron
    with patch.dict(cron.JOBS, {"cleaning-holds": lambda: 2}):
        resp = client.post("/cron/cleaning-holds/", HTTP_X_CRON_KEY="s3cret")
    assert resp.status_code == 200
    assert resp.json() == {"job": "cleaning-holds", "processed": 2}
