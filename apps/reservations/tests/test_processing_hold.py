"""APC-95 — Trip Processing takes a cleaning fee out of the order's hold first, and the last
clean held trip lets the hold go (spec 2026-10-27 §4, §5).

Stripe is mocked throughout and the key is blanked so an unmocked call can't reach it.
"""

from datetime import time, timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import stripe
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import holds, ledger, receipts, trip_money
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import ProcessingEvent, Reservation, TripReview

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
_types = count()


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


@pytest.fixture
def agent():
    return UserFactory(can_manage_payments=True)


def _order(trips=1, *, hold="500.00"):
    """A booked card order whose `trips` finished trips each ride a van with a cleaning fee,
    prepaid in full, with the order's cleaning hold AUTHORIZED for `hold`."""
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory())
    made = []
    for i in range(trips):
        vehicle = VehicleTypeFactory(name=f"Van {next(_types)}", cleaning_fee=Decimal("500"))
        trip = ReservationFactory(
            lead=lead,
            vehicle=vehicle,
            trip_status=S.DONE,
            trip_type="hourly",
            rate=Decimal("200"),
            hours=Decimal("3"),
            min_hours=0,
            pickup_date=timezone.localdate() - timedelta(days=trips - i),
            pickup_time=time(10, 0),
            pickup_timezone="America/Los_Angeles",
        )
        trip.booked_total = trip.line_total
        trip.save(update_fields=["booked_total"])
        made.append(trip)
    total = sum((t.line_total for t in made), Decimal("0.00"))
    plan = PaymentPlanFactory(
        lead=lead,
        quote_total=total,
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
        card_brand="visa",
        card_last4="4242",
    )
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=total,
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_full",
        idempotency_key=f"bal-{lead.pk}",
    )
    ledger.post_capture(
        lead=lead,
        amount=total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"p{lead.pk}",
    )
    held = plan.charges.create(
        kind=Charge.Kind.HOLD,
        amount=Decimal(hold),
        status=Charge.Status.AUTHORIZED,
        stripe_payment_intent_id="pi_hold",
        idempotency_key=f"hold-{lead.pk}",
        note="Cleaning hold",
        authorized_at=timezone.now(),
        capture_expires_at=timezone.now() + timedelta(days=6),
    )
    return lead, plan, made, held


def _dirty(trip, amount="250.00"):
    TripExtraFactory(
        reservation=trip, kind="cleaning", label="Cleaning fee", amount=Decimal(amount)
    )
    return Reservation.objects.get(pk=trip.pk)


def _intent(pi="pi_hold", *, status="succeeded", amount=25000):
    return MagicMock(
        id=pi,
        status=status,
        amount=amount,
        amount_received=amount if status == "succeeded" else 0,
        client_secret=f"{pi}_secret",
        metadata={},
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _diff(trip):
    return trip_money.money_for(Reservation.objects.get(pk=trip.pk)).difference


def _kinds(trip):
    return [e.kind for e in TripReview.objects.get(reservation=trip).events.all()]


def _never(*_args, **_kwargs):
    raise AssertionError("no card charge expected")


# --- capture first ---------------------------------------------------------------------------


def test_line_below_the_hold_captures_the_line_and_releases_the_rest(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")

    with (
        patch.object(stripe.PaymentIntent, "capture", return_value=_intent()) as capture,
        patch.object(stripe.PaymentIntent, "create", side_effect=_never),
        patch.object(stripe.PaymentIntent, "cancel") as cancel,
    ):
        review = processing.process(trip, user=agent, expected="250.00", collect="card")

    assert capture.call_args.kwargs["amount_to_capture"] == 25000
    cancel.assert_not_called()  # Stripe lets the uncaptured $250 go itself
    hold.refresh_from_db()
    assert (hold.status, hold.amount, hold.reservation) == ("succeeded", Decimal("250.00"), trip)
    assert review.is_processed
    assert _diff(trip) == Decimal("0.00")
    assert not plan.charges.filter(kind=Charge.Kind.TRIP).exists()
    assert _kinds(trip) == [ProcessingEvent.Kind.HOLD_CAPTURED, ProcessingEvent.Kind.PROCESSED]
    event = review.events.first()
    assert event.amount == Decimal("250.00") and event.charge == hold
    assert event.note == "Cleaning fee $250.00 taken from the $500.00 hold"


def test_line_above_the_hold_captures_all_and_collects_the_rest(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "650.00")

    with (
        patch.object(
            stripe.PaymentIntent, "capture", return_value=_intent(amount=50000)
        ) as capture,
        patch.object(
            stripe.PaymentIntent, "create", return_value=_intent("pi_trip", amount=15000)
        ) as create,
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent("pi_trip", amount=15000)
        ),
    ):
        review = processing.process(trip, user=agent, expected="650.00", collect="card")

    assert capture.call_args.kwargs["amount_to_capture"] == 50000
    assert create.call_args.kwargs["amount"] == 15000
    hold.refresh_from_db()
    assert (hold.status, hold.amount) == ("succeeded", Decimal("500.00"))
    assert plan.charges.get(kind=Charge.Kind.TRIP).amount == Decimal("150.00")
    assert review.is_processed and _diff(trip) == Decimal("0.00")
    assert _kinds(trip) == ["hold_captured", "charged", "processed"]


def test_expected_guard_uses_the_pre_capture_difference(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")

    with patch.object(stripe.PaymentIntent, "capture") as capture:
        with pytest.raises(processing.ProcessingError, match="price changed"):
            processing.process(trip, user=agent, expected="0.00", collect="card")

    capture.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


def test_a_cleaning_line_already_paid_for_is_not_taken_again(agent):
    """The line was on the price when the order booked (the balance paid it): the trip owes
    nothing, so nothing comes out of the hold."""
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    balance = plan.charges.get(kind=Charge.Kind.BALANCE)
    balance.amount += Decimal("250.00")
    balance.save(update_fields=["amount"])

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(stripe.PaymentIntent, "cancel"),
    ):
        processing.process(trip, user=agent, expected="0.00")

    capture.assert_not_called()


def test_ledger_posts_once_for_a_capture(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")

    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent()):
        processing.process(trip, user=agent, expected="250.00", collect="card")
    # The `succeeded` webhook for the same intent arrives afterwards: nothing more.
    holds.record_captured(plan, {"id": "pi_hold", "amount_received": 25000})
    with patch.object(stripe.PaymentIntent, "capture") as again:
        with pytest.raises(processing.ProcessingError, match="already processed"):
            processing.process(trip, user=agent, expected="0.00")
    again.assert_not_called()

    entries = JournalEntry.objects.filter(charge=hold)
    assert entries.count() == 1
    assert entries.get().reservation == trip


# --- a capture that can't happen -------------------------------------------------------------


def _lapsed_or_paid(pi, **_kwargs):
    """The hold reads back canceled by the bank; the new trip charge succeeded."""
    if pi == "pi_hold":
        return _intent(status="canceled")
    return _intent(pi, amount=25000)


def test_lapsed_hold_falls_through_to_the_card(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")
    err = stripe.error.InvalidRequestError("This PaymentIntent is canceled.", None)

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=_lapsed_or_paid),
        patch.object(
            stripe.PaymentIntent, "create", return_value=_intent("pi_trip", amount=25000)
        ) as create,
    ):
        review = processing.process(trip, user=agent, expected="250.00", collect="card")

    assert create.call_args.kwargs["amount"] == 25000
    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED and hold.reservation is None
    assert review.is_processed and _diff(trip) == Decimal("0.00")
    assert _kinds(trip) == ["hold_lapsed", "charged", "processed"]
    assert review.events.first().note == "Cleaning hold had lapsed; charged normally"


def test_unknown_capture_failure_stops_processing(agent):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")
    err = stripe.error.APIConnectionError("Network down")

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=err),
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent(status="requires_capture")
        ),
        patch.object(stripe.PaymentIntent, "create", side_effect=_never),
    ):
        with pytest.raises(processing.ProcessingError, match="could not be captured"):
            processing.process(trip, user=agent, expected="250.00", collect="card")

    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED
    assert not TripReview.objects.get(reservation=trip).is_processed
    assert not plan.charges.filter(kind=Charge.Kind.TRIP).exists()


# --- releasing -------------------------------------------------------------------------------


def test_no_line_on_last_held_trip_releases(agent):
    lead, plan, (trip,), hold = _order()

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(stripe.PaymentIntent, "cancel") as cancel,
    ):
        processing.process(trip, user=agent, expected="0.00")

    capture.assert_not_called()
    cancel.assert_called_once_with("pi_hold")
    hold.refresh_from_db()
    assert hold.status == Charge.Status.RELEASED
    assert hold.note == "all held trips processed"


def test_no_line_on_earlier_trip_keeps_the_hold(agent):
    lead, plan, (first, _second), hold = _order(2)

    with patch.object(stripe.PaymentIntent, "cancel") as cancel:
        processing.process(first, user=agent, expected="0.00")

    cancel.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


def test_a_release_stripe_refuses_never_undoes_the_processing(agent):
    lead, plan, (trip,), hold = _order()
    err = stripe.error.APIConnectionError("Network down")

    with (
        patch.object(stripe.PaymentIntent, "cancel", side_effect=err),
        patch.object(stripe.PaymentIntent, "retrieve", side_effect=err),
    ):
        review = processing.process(trip, user=agent, expected="0.00")

    assert review.is_processed
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED  # the hourly sweep lets it go


def test_second_trip_after_capture_touches_nothing(agent):
    """Review Focus 1: the dirty trip first ($250 of a $500 hold), then a clean one. The
    second must neither capture nor release: the hold is spent."""
    lead, plan, (dirty, clean), hold = _order(2)
    dirty = _dirty(dirty, "250.00")
    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent()):
        processing.process(dirty, user=agent, expected="250.00", collect="card")

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(stripe.PaymentIntent, "cancel") as cancel,
    ):
        processing.process(clean, user=agent, expected="0.00")

    capture.assert_not_called()
    cancel.assert_not_called()
    hold.refresh_from_db()
    assert (hold.status, hold.reservation) == ("succeeded", dirty)
    assert _kinds(clean) == ["processed"]


def test_a_second_dirty_trip_after_a_capture_is_charged_normally(agent):
    lead, plan, (first, second), hold = _order(2)
    first = _dirty(first, "250.00")
    second = _dirty(second, "300.00")
    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent()):
        processing.process(first, user=agent, expected="250.00", collect="card")

    with (
        patch.object(stripe.PaymentIntent, "capture") as capture,
        patch.object(
            stripe.PaymentIntent, "create", return_value=_intent("pi_trip", amount=30000)
        ) as create,
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent("pi_trip", amount=30000)
        ),
    ):
        processing.process(second, user=agent, expected="300.00", collect="card")

    capture.assert_not_called()
    assert create.call_args.kwargs["amount"] == 30000
    assert _kinds(second) == ["charged", "processed"]


# --- captured hold money behaves like trip money ---------------------------------------------


def _captured(agent, amount="250.00"):
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, amount)
    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent()):
        processing.process(trip, user=agent, expected=amount, collect="card")
    hold.refresh_from_db()
    return lead, plan, Reservation.objects.get(pk=trip.pk), hold


def test_captured_hold_is_refundable_like_a_trip_charge(agent):
    from apps.payments import services

    lead, plan, trip, hold = _captured(agent)
    # Only this trip's share of the order's money, plus its own captured hold.
    assert services.refundable_total(plan, trip=trip) == trip.booked_total + Decimal("250.00")
    sources = services._refundable_sources(plan, trip=trip)
    assert sources[0][0] == hold  # the trip's own money goes back first

    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_1")) as refund:
        refunded = trip_money.refund_trip(trip, Decimal("100.00"), reason="Not that dirty")

    assert refunded == Decimal("100.00")
    assert refund.call_args.kwargs["payment_intent"] == "pi_hold"
    assert refund.call_args.kwargs["amount"] == 10000
    row = plan.charges.get(kind=Charge.Kind.REFUND)
    assert row.reservation == trip and row.amount == Decimal("100.00")
    assert JournalEntry.objects.filter(charge=row).count() == 1
    assert services.refundable_total(plan, trip=trip) == trip.booked_total + Decimal("150.00")
    money = trip_money.money_for(Reservation.objects.get(pk=trip.pk))
    assert (money.charged, money.refunded) == (Decimal("250.00"), Decimal("100.00"))


def test_pane_shows_the_hold_sentence(client, agent):
    client.force_login(agent)
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")

    body = client.get(reverse("processing"), {"trip": trip.pk}).content.decode()
    assert "Cleaning hold $500.00 is on the card. A cleaning fee comes out of it first." in body

    with patch.object(stripe.PaymentIntent, "capture", return_value=_intent()):
        processing.process(trip, user=agent, expected="250.00", collect="card")
    hold.refresh_from_db()
    body = client.get(reverse("processing"), {"trip": trip.pk, "view": "processed"})
    body = body.content.decode()
    local = timezone.localtime(hold.captured_at, ZoneInfo(trip.pickup_timezone))
    stamp = f"{local:%b} {local.day}, {local:%-I:%M %p} {local.tzname()}"
    assert f"Cleaning hold captured $250.00 on {stamp}." in body
    assert "is on the card" not in body


def test_pane_says_nothing_without_a_hold(client, agent):
    client.force_login(agent)
    lead, plan, (trip,), hold = _order()
    hold.status = Charge.Status.RELEASED
    hold.save(update_fields=["status"])

    body = client.get(reverse("processing"), {"trip": trip.pk}).content.decode()

    assert "Cleaning hold" not in body


def test_history_and_receipt_show_the_capture(client, agent):
    client.force_login(agent)
    lead, plan, trip, hold = _captured(agent)

    body = client.get(reverse("processing"), {"trip": trip.pk, "view": "processed"})
    body = body.content.decode()
    assert "Cleaning hold captured $250.00" in body
    assert "Cleaning fee $250.00 taken from the $500.00 hold" in body

    assert receipts.is_receipt(hold)
    context = receipts.receipt_context(hold)
    assert context["lines"] == [("Cleaning fee", "$250.00")]
    email = render_to_string("email/trip_receipt.html", context)
    assert trip.reference in email and "Cleaning fee" in email and "$250.00" in email


def test_the_hold_row_carries_its_quickbooks_state_in_the_pane(client, agent):
    from apps.billing import qbo_receipts

    client.force_login(agent)
    lead, plan, trip, hold = _captured(agent)
    hold.qbo_sync_state = "error"
    hold.qbo_sync_error = "Customer is inactive."
    hold.save(update_fields=["qbo_sync_state", "qbo_sync_error"])

    with patch("apps.integrations.qbo.is_connected", return_value=True):
        body = client.get(reverse("processing"), {"trip": trip.pk, "view": "processed"})
    body = body.content.decode()

    assert "Customer is inactive." in body
    assert reverse("charge_qbo_sync", args=[hold.pk]) in body
    assert qbo_receipts.sync_state(hold, connected=True) is not None


# --- Task 5 ride-alongs ----------------------------------------------------------------------


def test_a_hold_another_trip_just_used_falls_through_to_the_card(agent):
    """Two trips on one order processed at once: the other won the capture. This trip's
    read of the hold was AUTHORIZED, the locked re-read is not, so its fee goes on the card
    the normal way instead of stopping the processing."""
    lead, plan, (first, second), hold = _order(2)
    first = _dirty(first, "250.00")
    stale = Charge.objects.get(pk=hold.pk)
    hold.status = Charge.Status.SUCCEEDED
    hold.reservation = second
    hold.captured_at = timezone.now()
    hold.save(update_fields=["status", "reservation", "captured_at"])

    with (
        patch("apps.payments.holds.current_hold", return_value=stale),
        patch.object(stripe.PaymentIntent, "capture", side_effect=_never) as capture,
        patch.object(
            stripe.PaymentIntent, "create", return_value=_intent("pi_trip", amount=25000)
        ) as create,
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent("pi_trip", amount=25000)
        ),
    ):
        review = processing.process(first, user=agent, expected="250.00", collect="card")

    capture.assert_not_called()
    assert create.call_args.kwargs["amount"] == 25000
    assert review.is_processed and _diff(first) == Decimal("0.00")
    assert _kinds(first) == ["hold_used", "charged", "processed"]
    assert review.events.first().note == "Cleaning hold already used; charged normally"
    hold.refresh_from_db()
    assert hold.reservation == second


def test_history_shows_when_the_hold_was_captured_not_placed(client, agent):
    client.force_login(agent)
    lead, plan, trip, hold = _captured(agent)
    placed = timezone.now() - timedelta(days=2)
    Charge.objects.filter(pk=hold.pk).update(created_at=placed)
    hold.refresh_from_db()

    body = client.get(reverse("processing"), {"trip": trip.pk, "view": "processed"})
    body = body.content.decode()

    def clock(moment):
        local = timezone.localtime(moment, ZoneInfo(trip.pickup_timezone))
        return f"{local:%b} {local.day}, {local:%-I:%M %p}"

    charges = body.split("Charges on this trip", 1)[1].split("History", 1)[0]
    assert clock(hold.captured_at) in charges
    assert clock(placed) not in charges


def test_retrying_the_trip_after_a_capture_charges_only_the_remainder(agent):
    """$650 line: the $500 capture succeeds, the $150 card charge is declined. The retry
    owes $150, charges that, and never captures again."""
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "650.00")
    declined = stripe.error.CardError("Your card was declined.", "card", "card_declined")

    with (
        patch.object(stripe.PaymentIntent, "capture", return_value=_intent(amount=50000)),
        patch.object(stripe.PaymentIntent, "create", side_effect=declined),
    ):
        with pytest.raises(processing.ProcessingError):
            processing.process(trip, user=agent, expected="650.00", collect="card")

    hold.refresh_from_db()
    assert (hold.status, hold.amount) == ("succeeded", Decimal("500.00"))
    assert _diff(trip) == Decimal("150.00")
    assert not TripReview.objects.get(reservation=trip).is_processed

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=_never) as capture,
        patch.object(
            stripe.PaymentIntent, "create", return_value=_intent("pi_trip2", amount=15000)
        ) as create,
        patch.object(
            stripe.PaymentIntent, "retrieve", return_value=_intent("pi_trip2", amount=15000)
        ),
    ):
        review = processing.process(trip, user=agent, expected="150.00", collect="card")

    capture.assert_not_called()
    assert create.call_args.kwargs["amount"] == 15000
    assert review.is_processed and _diff(trip) == Decimal("0.00")
    assert JournalEntry.objects.filter(charge=hold).count() == 1


def test_a_pay_link_with_outbound_off_is_refused_before_the_capture(agent, settings):
    settings.OUTBOUND_ENABLED = False
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "650.00")

    with patch.object(stripe.PaymentIntent, "capture", side_effect=_never) as capture:
        with pytest.raises(processing.ProcessingError, match="Outbound messaging is off"):
            processing.process(trip, user=agent, expected="650.00", collect="link")

    capture.assert_not_called()
    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED


def test_a_double_submit_of_one_trip_records_one_capture(agent):
    """The first submit captured and tagged the hold for this trip, then lost the race to
    its own books; the second submit finishes the books but is not a second capture."""
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, "250.00")
    Charge.objects.filter(pk=hold.pk).update(
        captured_at=timezone.now(), reservation=trip, amount=Decimal("250.00")
    )

    with (
        patch.object(stripe.PaymentIntent, "capture", side_effect=_never) as capture,
        patch.object(stripe.PaymentIntent, "create", side_effect=_never),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()),
    ):
        review = processing.process(trip, user=agent, expected="250.00", collect="card")

    capture.assert_not_called()
    assert review.is_processed and _diff(trip) == Decimal("0.00")
    assert ProcessingEvent.Kind.HOLD_CAPTURED not in _kinds(trip)
    assert JournalEntry.objects.filter(charge=hold).count() == 1


# --- what the pane says comes from the hold and from the card (HOLD-07, HOLD-12) -------------


def _split(client, trip):
    resp = client.get(reverse("processing"), {"trip": trip.pk})
    return resp.context["from_hold"], resp.context["from_card"]


@pytest.mark.parametrize(
    ("line", "from_hold", "from_card"),
    [("250.00", "250.00", "0.00"), ("650.00", "500.00", "150.00")],
)
def test_pane_splits_what_the_hold_pays_from_what_the_card_pays(
    client, agent, line, from_hold, from_card
):
    client.force_login(agent)
    lead, plan, (trip,), hold = _order()
    trip = _dirty(trip, line)

    assert _split(client, trip) == (Decimal(from_hold), Decimal(from_card))


def test_pane_puts_everything_on_the_card_without_a_hold(client, agent):
    client.force_login(agent)
    lead, plan, (trip,), hold = _order()
    hold.status = Charge.Status.RELEASED
    hold.save(update_fields=["status"])
    trip = _dirty(trip, "250.00")

    assert _split(client, trip) == (Decimal("0.00"), Decimal("250.00"))


def test_pane_counts_an_outside_capture_processing_will_adopt(client, agent):
    client.force_login(agent)
    lead, plan, (trip,), hold = _order()
    hold.captured_at = timezone.now()
    hold.amount = Decimal("400.00")  # what Stripe took
    hold.save(update_fields=["captured_at", "amount"])
    trip = _dirty(trip, "650.00")

    assert _split(client, trip) == (Decimal("400.00"), Decimal("250.00"))
