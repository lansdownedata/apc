"""APC-95 — the cleaning hold (spec 2026-10-27 §2, §4).

One manual-capture card hold per held order: placed off-session for the highest cleaning
fee, captured once for a dirty trip, released otherwise. Stripe is mocked throughout and
the key is blanked so an unmocked call can't reach it.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import CompanyFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import holds, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)
CAPTURE_BEFORE = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
_types = count()


@pytest.fixture(autouse=True)
def _no_real_stripe(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _order(*fees, status=Lead.Status.BOOKED, card=True):
    """A card order with one trip per fee (each its own vehicle type), a day apart."""
    lead = LeadFactory(status=status)
    trips = []
    for i, fee in enumerate(fees):
        vehicle = VehicleTypeFactory(name=f"Type {next(_types)}", cleaning_fee=Decimal(fee))
        trips.append(
            ReservationFactory(
                lead=lead,
                vehicle=vehicle,
                pickup_date=timezone.localdate() + timedelta(days=1 + i),
                pickup_time=time(9, 0),
                pickup_timezone="America/New_York",
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


def _intent(pi_id="pi_hold", *, status="requires_capture", amount=50000, capture_before=None):
    latest = (
        MagicMock(
            payment_method_details=MagicMock(
                card=MagicMock(capture_before=int(capture_before.timestamp()))
            )
        )
        if capture_before
        else "ch_1"
    )
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        amount_received=amount if status == "succeeded" else 0,
        latest_charge=latest,
        metadata={"kind": "hold"},
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _placed(*fees):
    lead, plan, trips = _order(*fees)
    with patch.object(
        stripe.PaymentIntent, "create", return_value=_intent(capture_before=CAPTURE_BEFORE)
    ):
        hold = holds.place_hold(lead, now=NOW)
    return lead, plan, trips, hold


# --- which orders are held ----------------------------------------------------------------


def test_a_booked_card_order_with_a_van_is_held():
    lead, _plan, _trips = _order("500.00")
    assert holds.eligible(lead) is True


def test_a_farmed_in_order_is_not_held():
    lead, _plan, _trips = _order("500.00")
    lead.contact.company = CompanyFactory(is_affiliate_account=True)
    lead.contact.save(update_fields=["company"])
    lead.refresh_from_db()
    assert holds.eligible(lead) is False


def test_an_order_billed_to_an_account_is_not_held():
    lead, _plan, _trips = _order("500.00")
    lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
    lead.save(update_fields=["billing_group"])
    assert holds.eligible(lead) is False


def test_an_order_with_no_held_vehicle_is_not_held():
    lead, _plan, _trips = _order("0.00", "0.00")
    assert holds.eligible(lead) is False


def test_an_order_whose_held_trips_are_all_cancelled_is_not_held():
    lead, _plan, (van, _sedan) = _order("500.00", "0.00")
    van.trip_status = Reservation.TripStatus.CANCELLED
    van.save(update_fields=["trip_status"])
    assert holds.eligible(lead) is False
    assert holds.held_trips(lead) == []


def test_a_quote_is_not_held():
    lead, _plan, _trips = _order("500.00", status=Lead.Status.QUOTED)
    assert holds.eligible(lead) is False


def test_held_trips_are_the_live_fee_trips_in_pickup_order():
    lead, _plan, (a, _sedan, c) = _order("250.00", "0.00", "500.00")
    assert holds.held_trips(lead) == [a, c]


def test_amount_is_the_highest_fee_not_the_sum():
    lead, _plan, _trips = _order("250.00", "500.00", "0.00")
    assert holds.hold_amount(lead) == Decimal("500.00")


def test_amount_is_zero_with_no_held_vehicle():
    lead, _plan, _trips = _order("0.00")
    assert holds.hold_amount(lead) == Decimal("0")


# --- placing --------------------------------------------------------------------------------


def test_place_hold_creates_one_authorized_row_and_calls_stripe_manual_capture():
    lead, plan, _trips = _order("250.00", "500.00")

    with patch.object(
        stripe.PaymentIntent, "create", return_value=_intent(capture_before=CAPTURE_BEFORE)
    ) as create:
        hold = holds.place_hold(lead, now=NOW)

    kwargs = create.call_args.kwargs
    assert kwargs["amount"] == 50000
    assert kwargs["capture_method"] == "manual"
    assert kwargs["off_session"] is True and kwargs["confirm"] is True
    assert (kwargs["customer"], kwargs["payment_method"]) == ("cus_1", "pm_1")
    assert kwargs["payment_method_types"] == ["card"]
    assert kwargs["metadata"] == {
        "lead_id": str(lead.pk),
        "kind": "hold",
        "charge_id": str(hold.pk),
    }
    assert kwargs["idempotency_key"] == hold.idempotency_key
    hold.refresh_from_db()
    assert (hold.kind, hold.status, hold.amount) == ("hold", "authorized", Decimal("500.00"))
    assert hold.stripe_payment_intent_id == "pi_hold"
    assert hold.authorized_at == NOW
    assert hold.capture_expires_at == CAPTURE_BEFORE
    assert hold.reservation is None and hold.note == "Cleaning hold"
    assert (hold.card_brand, hold.card_last4) == ("visa", "4242")
    assert holds.current_hold(lead) == hold
    assert plan.charges.count() == 1


def test_without_capture_before_the_window_is_our_estimate():
    lead, _plan, _trips = _order("500.00")
    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()):
        hold = holds.place_hold(lead, now=NOW)
    assert hold.capture_expires_at == NOW + timedelta(days=services.AUTH_HOLD_DAYS)


def test_place_hold_twice_is_one_row():
    lead, plan, _trips = _order("500.00")

    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as create:
        first = holds.place_hold(lead, now=NOW)
        second = holds.place_hold(lead, now=NOW)

    assert create.call_count == 1
    assert first == second
    assert plan.charges.filter(kind=Charge.Kind.HOLD).count() == 1


def test_place_hold_without_card_opens_one_task_and_notifies_without_a_row():
    lead, plan, _trips = _order("500.00", card=False)

    with patch.object(stripe.PaymentIntent, "create") as create:
        assert holds.place_hold(lead, now=NOW) is None
        assert holds.place_hold(lead, now=NOW) is None

    create.assert_not_called()
    assert not plan.charges.exists()
    task = Task.objects.get(lead=lead, kind="cleaning_hold_failed")
    assert task.status == Task.Status.OPEN
    assert task.note == "Cleaning hold not placed: no card on file"
    note = Notification.objects.get(lead=lead, kind=Notification.Kind.HOLD_FAILED)
    assert note.title == "Cleaning hold not placed: no card on file"
    lead.refresh_from_db()
    assert lead.has_alert is True


def test_declined_hold_records_failed_opens_task_and_is_not_retried():
    lead, plan, _trips = _order("500.00")
    err = stripe.error.CardError("Your card was declined.", "card", "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=err) as create:
        failed = holds.place_hold(lead, now=NOW)
        again = holds.place_hold(lead, now=NOW)

    assert create.call_count == 1
    assert again == failed
    failed.refresh_from_db()
    assert (failed.kind, failed.status) == ("hold", "failed")
    assert failed.failure_reason == "Your card was declined."
    assert holds.current_hold(lead) is None
    assert holds.any_hold(lead) == failed
    task = Task.objects.get(lead=lead, kind="cleaning_hold_failed")
    assert task.note == "Cleaning hold declined: Your card was declined."
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.HOLD_FAILED).count() == 1
    lead.refresh_from_db()
    plan.refresh_from_db()
    assert lead.has_alert is True
    assert plan.balance_status != "failed" and plan.fail_reason == ""


def test_place_hold_ineligible_does_nothing():
    lead, plan, _trips = _order("500.00", status=Lead.Status.QUOTED)

    with patch.object(stripe.PaymentIntent, "create") as create:
        assert holds.place_hold(lead, now=NOW) is None

    create.assert_not_called()
    assert not plan.charges.exists()
    assert not Task.objects.filter(lead=lead, kind="cleaning_hold_failed").exists()


def test_placing_posts_nothing_to_the_ledger():
    lead, plan, _trips, hold = _placed("500.00")
    deposit, balance = plan.deposit_status, plan.balance_status
    with patch.object(stripe.PaymentIntent, "cancel"):
        holds.release_hold(lead)
    holds.mark_expired(hold)

    assert not JournalEntry.objects.filter(lead=lead).exists()
    plan.refresh_from_db()
    assert (plan.deposit_status, plan.balance_status) == (deposit, balance)


# --- capturing ------------------------------------------------------------------------------


def test_capture_takes_min_of_line_and_hold_and_posts_once():
    lead, _plan, (trip,), hold = _placed("500.00")
    captured_intent = _intent(status="succeeded", amount=50000)

    with patch.object(stripe.PaymentIntent, "capture", return_value=captured_intent) as capture:
        charge = holds.capture_hold(trip, Decimal("250.00"))

    capture.assert_called_once_with(
        "pi_hold", amount_to_capture=25000, idempotency_key=f"{hold.idempotency_key}-capture"
    )
    assert charge.pk == hold.pk
    charge.refresh_from_db()
    assert (charge.kind, charge.status) == ("hold", "succeeded")
    assert charge.amount == Decimal("250.00") and charge.reservation == trip
    assert charge.captured_at is not None
    assert charge.note == "Cleaning fee from the hold"
    assert charge.receipt_lines == [["Cleaning fee", "250.00"]]
    entries = JournalEntry.objects.filter(charge=charge)
    assert entries.count() == 1
    entry = entries.get()
    assert entry.kind == JournalEntry.Kind.BALANCE_CAPTURED and entry.reservation == trip
    assert entry.is_balanced

    with patch.object(stripe.PaymentIntent, "capture") as capture_again:
        with pytest.raises(services.PaymentError):
            holds.capture_hold(trip, Decimal("250.00"))
    capture_again.assert_not_called()
    assert JournalEntry.objects.filter(charge=charge).count() == 1


def test_capture_above_the_hold_takes_only_the_hold():
    lead, _plan, (trip,), _hold = _placed("500.00")
    with patch.object(
        stripe.PaymentIntent, "capture", return_value=_intent(status="succeeded")
    ) as capture:
        charge = holds.capture_hold(trip, Decimal("650.00"))
    assert capture.call_args.kwargs["amount_to_capture"] == 50000
    assert charge.amount == Decimal("500.00")


def test_capture_without_a_hold_refuses():
    lead, _plan, (trip,) = _order("500.00")
    with pytest.raises(services.PaymentError, match="No cleaning hold to capture"):
        holds.capture_hold(trip, Decimal("100.00"))


# --- releasing and expiry -------------------------------------------------------------------


def test_release_cancels_the_intent_once():
    lead, _plan, _trips, hold = _placed("500.00")

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        released = holds.release_hold(lead, reason="all held trips processed")
        again = holds.release_hold(lead, reason="all held trips processed")

    cancel.assert_called_once_with("pi_hold")
    assert released == again == hold
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED
    assert hold.note == "all held trips processed"


def test_release_without_a_hold_is_none():
    lead, _plan, _trips = _order("500.00")
    assert holds.release_hold(lead) is None


@pytest.mark.parametrize("status", [Charge.Status.SUCCEEDED, Charge.Status.EXPIRED])
def test_release_leaves_captured_or_expired_alone(status):
    lead, _plan, _trips, hold = _placed("500.00")
    hold.status = status
    hold.save(update_fields=["status"])

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        result = holds.release_hold(lead, reason="whatever")

    cancel.assert_not_called()
    assert result == hold
    hold.refresh_from_db()
    assert hold.status == status and hold.note == "Cleaning hold"


def test_mark_expired_only_from_authorized():
    lead, _plan, _trips, hold = _placed("500.00")
    assert holds.mark_expired(hold).status == Charge.Status.EXPIRED

    lead2, _plan2, _trips2, released = _placed("500.00")
    with patch.object(stripe.PaymentIntent, "cancel"):
        holds.release_hold(lead2)
    released.refresh_from_db()
    assert holds.mark_expired(released).status == Charge.Status.RELEASED
    released.refresh_from_db()
    assert released.status == Charge.Status.RELEASED


def test_a_captured_hold_is_the_trips_receipt():
    from apps.payments import receipts

    _lead, _plan, (trip,), _hold = _placed("500.00")
    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent(status="succeeded")):
        charge = holds.capture_hold(trip, Decimal("250.00"))
    charge.refresh_from_db()
    assert charge.receipt_due_at is not None
    assert receipts.is_receipt(charge)


def test_release_of_a_hold_the_bank_already_dropped_marks_it_expired():
    lead, _plan, _trips, hold = _placed("500.00")
    err = stripe.error.InvalidRequestError("This PaymentIntent is already canceled.", None)

    with (
        patch.object(stripe.PaymentIntent, "cancel", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent(status="canceled")),
    ):
        result = holds.release_hold(lead, reason="order cancelled")

    assert result.status == Charge.Status.EXPIRED
    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED


def test_a_release_stripe_refuses_otherwise_raises_and_keeps_the_hold():
    lead, _plan, _trips, hold = _placed("500.00")
    err = stripe.error.APIConnectionError("Network down")

    with (
        patch.object(stripe.PaymentIntent, "cancel", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=err),
    ):
        with pytest.raises(services.PaymentError):
            holds.release_hold(lead)

    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


# --- fix round: a hold never touches the balance; transient Stripe failures -----------------


def test_a_hold_capture_leaves_the_overtime_task_open():
    lead, _plan, (trip,), _hold = _placed("500.00")
    task = Task.objects.create(
        lead=lead,
        reservation=trip,
        kind="overtime_invoiced",
        status=Task.Status.OPEN,
        opens_at=timezone.now(),
    )
    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent(status="succeeded")):
        holds.capture_hold(trip, Decimal("250.00"))
    task.refresh_from_db()
    assert task.status == Task.Status.OPEN


def _event(etype, lead, pi_id="pi_hold", **extra):
    return {
        "type": etype,
        "data": {
            "object": {"id": pi_id, "metadata": {"lead_id": str(lead.pk), "kind": "hold"}, **extra}
        },
    }


def test_a_failed_webhook_for_a_hold_leaves_the_balance_alone():
    from apps.payments import webhooks

    lead, plan, _trips = _order("500.00")
    err = stripe.error.CardError("Your card was declined.", "card", "card_declined")
    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        holds.place_hold(lead, now=NOW)
    balance = plan.balance_status

    webhooks.process_stripe_event(
        _event(
            "payment_intent.payment_failed",
            lead,
            last_payment_error={"message": "Your card was declined."},
        )
    )

    plan.refresh_from_db()
    assert (plan.balance_status, plan.fail_reason) == (balance, "")
    assert not Notification.objects.filter(lead=lead, kind=Notification.Kind.BALANCE_FAILED)
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.HOLD_FAILED).count() == 1


def test_a_succeeded_webhook_finishes_a_capture_whose_books_never_did():
    from apps.payments import webhooks

    lead, _plan, (trip,), hold = _placed("500.00")
    hold.reservation, hold.amount, hold.captured_at = trip, Decimal("250.00"), timezone.now()
    hold.save(update_fields=["reservation", "amount", "captured_at"])
    succeeded = _intent(status="succeeded", amount=50000)

    with patch.object(stripe.PaymentIntent, "retrieve", return_value=succeeded):
        webhooks.process_stripe_event(_event("payment_intent.succeeded", lead))
        webhooks.process_stripe_event(_event("payment_intent.succeeded", lead))

    hold.refresh_from_db()
    assert (hold.kind, hold.status) == ("hold", "succeeded")
    entries = JournalEntry.objects.filter(charge=hold)
    assert entries.count() == 1 and entries.get().reservation == trip


def test_a_network_error_leaves_the_hold_pending_and_quiet():
    lead, plan, _trips = _order("500.00")
    err = stripe.error.APIConnectionError("Network down")

    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        result = holds.place_hold(lead, now=NOW)

    assert (result.status, result.stripe_payment_intent_id) == ("pending", "")
    assert not Task.objects.filter(lead=lead, kind="cleaning_hold_failed").exists()
    assert not Notification.objects.filter(lead=lead).exists()


def test_the_next_run_resends_under_the_same_key():
    lead, plan, _trips = _order("500.00")
    err = stripe.error.APIConnectionError("Network down")
    with patch.object(stripe.PaymentIntent, "create", side_effect=err) as first:
        pending = holds.place_hold(lead, now=NOW)

    with patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as second:
        hold = holds.place_hold(lead, now=NOW)

    assert first.call_count == 1 and second.call_count == 1
    assert second.call_args.kwargs["idempotency_key"] == pending.idempotency_key
    assert first.call_args.kwargs["idempotency_key"] == pending.idempotency_key
    assert hold.pk == pending.pk and hold.status == Charge.Status.AUTHORIZED
    assert plan.charges.filter(kind=Charge.Kind.HOLD).count() == 1


def test_a_hold_created_but_not_saved_is_finished_from_stripe():
    lead, plan, _trips = _order("500.00")
    row = plan.record_charge(kind=Charge.Kind.HOLD, amount=Decimal("500.00"), note="Cleaning hold")
    row.stripe_payment_intent_id = "pi_hold"
    row.save(update_fields=["stripe_payment_intent_id"])

    with (
        patch.object(stripe.PaymentIntent, "create") as create,
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent(capture_before=CAPTURE_BEFORE)
        ) as retrieve,
    ):
        hold = holds.place_hold(lead, now=NOW)

    create.assert_not_called()
    assert retrieve.call_args.args[0] == "pi_hold"
    assert hold.pk == row.pk and hold.status == Charge.Status.AUTHORIZED
    assert hold.capture_expires_at == CAPTURE_BEFORE


def test_an_intent_not_awaiting_capture_stays_pending():
    lead, _plan, _trips = _order("500.00")
    with patch.object(
        stripe.PaymentIntent, "create", return_value=_intent(status="requires_action")
    ):
        hold = holds.place_hold(lead, now=NOW)
    assert (hold.status, hold.stripe_payment_intent_id) == ("pending", "pi_hold")
    assert hold.authorized_at is None


def test_capture_sends_its_own_idempotency_key():
    _lead, _plan, (trip,), hold = _placed("500.00")
    with patch.object(
        stripe.PaymentIntent, "capture", return_value=_intent(status="succeeded")
    ) as capture:
        holds.capture_hold(trip, Decimal("250.00"))
    assert capture.call_args.kwargs["idempotency_key"] == f"{hold.idempotency_key}-capture"


def test_a_capture_whose_reply_was_lost_recovers_and_posts_once():
    _lead, _plan, (trip,), hold = _placed("500.00")
    err = stripe.error.APIConnectionError("Network down")
    captured = _intent(status="succeeded")
    captured.amount_received = 25000

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=captured),
    ):
        # Asked for 400; Stripe took 250 (the first, lost capture): Stripe's figure wins.
        charge = holds.capture_hold(trip, Decimal("400.00"))

    charge.refresh_from_db()
    assert (charge.status, charge.amount, charge.reservation) == (
        "succeeded",
        Decimal("250.00"),
        trip,
    )
    assert charge.captured_at is not None
    assert JournalEntry.objects.filter(charge=hold).count() == 1


def test_capturing_a_hold_the_bank_already_released_marks_it_expired():
    _lead, _plan, (trip,), hold = _placed("500.00")
    err = stripe.error.InvalidRequestError("This PaymentIntent is canceled.", None)

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent(status="canceled")),
    ):
        with pytest.raises(services.PaymentError, match="bank released the cleaning hold"):
            holds.capture_hold(trip, Decimal("250.00"))

    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED and hold.reservation is None
    assert not JournalEntry.objects.filter(charge=hold).exists()


def test_a_capture_stripe_refuses_otherwise_raises_and_keeps_the_hold():
    _lead, _plan, (trip,), hold = _placed("500.00")
    err = stripe.error.APIConnectionError("Network down")
    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=err),
    ):
        with pytest.raises(services.PaymentError):
            holds.capture_hold(trip, Decimal("250.00"))
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED and hold.captured_at is None


def test_a_capture_already_taken_at_stripe_is_finished_not_taken_again():
    _lead, _plan, (trip,), hold = _placed("500.00")
    hold.reservation, hold.amount, hold.captured_at = trip, Decimal("250.00"), timezone.now()
    hold.save(update_fields=["reservation", "amount", "captured_at"])

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent(status="succeeded")),
    ):
        charge = holds.capture_hold(trip, Decimal("400.00"))

    capture.assert_not_called()
    charge.refresh_from_db()
    assert (charge.status, charge.amount) == ("succeeded", Decimal("250.00"))
    assert JournalEntry.objects.filter(charge=hold).count() == 1


# --- fix round 2: booked only for what was captured, whoever finishes it --------------------


def _succeeded_event(lead, amount_received):
    return _event("payment_intent.succeeded", lead, amount_received=amount_received)


def test_a_succeeded_webhook_before_the_capture_is_saved_books_nothing_yet():
    from apps.payments import webhooks

    lead, _plan, _trips, hold = _placed("500.00")

    webhooks.process_stripe_event(_succeeded_event(lead, 25000))

    hold.refresh_from_db()
    assert (hold.status, hold.amount, hold.reservation) == (
        Charge.Status.AUTHORIZED,
        Decimal("250.00"),
        None,
    )
    assert hold.captured_at is not None
    assert not JournalEntry.objects.filter(charge=hold).exists()


def test_a_later_capture_adopts_a_hold_the_webhook_saw_captured():
    from apps.payments import webhooks

    lead, _plan, (trip,), hold = _placed("500.00")
    err = stripe.error.APIConnectionError("Network down")
    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=err),
    ):
        with pytest.raises(services.PaymentError):
            holds.capture_hold(trip, Decimal("400.00"))
    webhooks.process_stripe_event(_succeeded_event(lead, 25000))

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(
            stripe.PaymentIntent,
            "retrieve",
            return_value=_intent(status="succeeded", amount=50000),
        ),
        patch("apps.payments.receipts.queue") as queue,
    ):
        charge = holds.capture_hold(trip, Decimal("400.00"))

    capture.assert_not_called()
    charge.refresh_from_db()
    assert (charge.status, charge.amount, charge.reservation) == (
        Charge.Status.SUCCEEDED,
        Decimal("250.00"),
        trip,
    )
    assert charge.receipt_lines == [["Cleaning fee", "250.00"]]
    assert charge.receipt_due_at is not None
    queue.assert_called_once()
    entries = JournalEntry.objects.filter(charge=hold)
    assert entries.count() == 1 and entries.get().reservation == trip


def test_a_succeeded_webhook_racing_the_capture_still_books_the_trip():
    from apps.payments import webhooks

    lead, _plan, (trip,), hold = _placed("500.00")

    def stripe_replies_and_the_webhook_lands(*args, **kwargs):
        webhooks.process_stripe_event(_succeeded_event(lead, 25000))
        return _intent(status="succeeded", amount=50000)

    with (
        patch.object(
            stripe.PaymentIntent, "capture", side_effect=stripe_replies_and_the_webhook_lands
        ),
        patch.object(
            stripe.PaymentIntent,
            "retrieve",
            return_value=_intent(status="succeeded", amount=50000),
        ),
        patch("apps.payments.receipts.queue") as queue,
    ):
        charge = holds.capture_hold(trip, Decimal("250.00"))

    charge.refresh_from_db()
    assert (charge.status, charge.amount, charge.reservation) == (
        Charge.Status.SUCCEEDED,
        Decimal("250.00"),
        trip,
    )
    queue.assert_called_once()
    entries = JournalEntry.objects.filter(charge=hold)
    assert entries.count() == 1 and entries.get().reservation == trip


def test_capture_locks_the_hold_row():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _lead, _plan, (trip,), _hold = _placed("500.00")
    seen = []

    def capture(*args, **kwargs):
        seen.extend(q["sql"] for q in ctx.captured_queries)
        return _intent(status="succeeded")

    with (
        CaptureQueriesContext(connection) as ctx,
        patch.object(stripe.PaymentIntent, "capture", side_effect=capture),
    ):
        holds.capture_hold(trip, Decimal("250.00"))

    # Held before Stripe is asked, so a webhook for this intent waits for the save.
    assert any("payments_charge" in sql and "FOR UPDATE" in sql for sql in seen)


def test_record_payment_refuses_to_book_a_hold_without_its_trip():
    _lead, plan, _trips, hold = _placed("500.00")
    with pytest.raises(services.PaymentError):
        services.record_payment(
            plan, hold.stripe_payment_intent_id, intent=_intent(status="succeeded")
        )
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED
    assert not JournalEntry.objects.filter(charge=hold).exists()


@pytest.mark.parametrize("state", ["canceled", "succeeded"])
def test_a_placement_stripe_already_settled_fails_once_and_stops(state):
    lead, plan, _trips = _order("500.00")
    row = plan.record_charge(kind=Charge.Kind.HOLD, amount=Decimal("500.00"), note="Cleaning hold")
    row.stripe_payment_intent_id = "pi_hold"
    row.save(update_fields=["stripe_payment_intent_id"])

    with patch.object(
        stripe.PaymentIntent, "retrieve", return_value=_intent(status=state)
    ) as retrieve:
        first = holds.place_hold(lead, now=NOW)
        second = holds.place_hold(lead, now=NOW)

    assert retrieve.call_count == 1
    assert first == second and first.status == Charge.Status.FAILED
    task = Task.objects.get(lead=lead, kind="cleaning_hold_failed")
    assert task.note == f"Cleaning hold could not be placed: {state}"
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.HOLD_FAILED).count() == 1


def test_an_idempotency_conflict_on_the_resend_fails_once_and_stops():
    lead, _plan, _trips = _order("500.00")
    with patch.object(
        stripe.PaymentIntent, "create", side_effect=stripe.error.APIConnectionError("down")
    ):
        holds.place_hold(lead, now=NOW)
    conflict = stripe.error.IdempotencyError("Keys for idempotent requests can only be used...")

    with patch.object(stripe.PaymentIntent, "create", side_effect=conflict) as create:
        first = holds.place_hold(lead, now=NOW)
        second = holds.place_hold(lead, now=NOW)

    assert create.call_count == 1
    assert first == second and first.status == Charge.Status.FAILED
    task = Task.objects.get(lead=lead, kind="cleaning_hold_failed")
    assert task.note.startswith("Cleaning hold could not be placed: ")
