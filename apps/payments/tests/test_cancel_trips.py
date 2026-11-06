"""Cancel & refund by trip (APC-127, spec 2026-11-06-per-trip-cancel-refund-design.md)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.dispatch import services as dispatch_services
from apps.dispatch.models import Assignment
from apps.leads.models import Lead
from apps.messaging import events
from apps.messaging.factories import MessageRowFactory
from apps.messaging.models import TouchPoint
from apps.payments import ledger, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

REFUND = "apps.payments.services.stripe.Refund.create"


def _plan(collected="1335.00"):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.reservations.all().delete()  # the factory's own trip; each test adds its own
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status", "updated_at"])
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=Decimal(collected),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id=f"pi_bal_{plan.pk}",
        idempotency_key=f"seed-bal-{plan.pk}",
    )
    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal(collected),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"cap-bal-{plan.pk}",
    )
    return plan


def _trip(lead, rate="1335.00", days=10):
    when = timezone.now() + timedelta(days=days)
    return TransferReservationFactory(
        lead=lead, rate=Decimal(rate), pickup_date=when.date(), pickup_time=when.time()
    )


def _admin():
    return UserFactory(role=User.Role.OWNER_ADMIN)


def _agent():
    return UserFactory(role=User.Role.AGENT, can_manage_payments=True)


def _refunds():
    return patch(REFUND, side_effect=lambda **kw: MagicMock(id=f"re_{kw['amount']}"))


# --- shares ------------------------------------------------------------------------------


def test_each_trips_share_is_its_own_money_plus_its_slice_of_the_order_level_money():
    plan = _plan("1000.00")
    a, b = _trip(plan.lead, "300.00"), _trip(plan.lead, "100.00")
    plan.charges.create(
        kind=Charge.Kind.TRIP,
        reservation=a,
        amount=Decimal("50.00"),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_trip_a",
        idempotency_key="seed-trip-a",
    )
    shares = services.cancel_shares(plan.lead)
    assert shares == {a.pk: Decimal("800.00"), b.pk: Decimal("250.00")}


def test_the_shares_add_up_to_everything_refundable_with_the_rounding_on_the_last_trip():
    plan = _plan("100.00")
    trips = [_trip(plan.lead, "10.00", days=d) for d in (3, 5, 7)]
    shares = services.cancel_shares(plan.lead)
    assert [shares[t.pk] for t in trips] == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    assert sum(shares.values()) == services.refundable_total(plan)


def test_a_cancelled_trip_has_no_share_and_no_longer_counts_toward_the_order():
    plan = _plan("1000.00")
    a, b = _trip(plan.lead, "300.00"), _trip(plan.lead, "100.00")
    Reservation.objects.filter(pk=a.pk).update(
        trip_status=Reservation.TripStatus.CANCELLED,
        revenue_status=Reservation.RevenueStatus.REVERSED,
    )
    assert services.cancel_shares(plan.lead) == {b.pk: Decimal("1000.00")}
    assert plan.lead.quote_total == b.line_total


# --- cancelling ----------------------------------------------------------------------------


def test_cancelling_one_trip_refunds_its_share_and_keeps_the_order_booked():
    plan = _plan()
    a, b = _trip(plan.lead), _trip(plan.lead)
    offer = dispatch_services.send_offer(a, VendorFactory(), payout=Decimal("900.00"))
    row = MessageRowFactory(trigger="pickup", recipients=["booking_contact"], send_email=False)
    queued = {
        t.pk: TouchPoint.objects.create(
            lead=plan.lead, reservation=t, message=row, kind=row.key, scheduled_for=timezone.now()
        )
        for t in (a, b)
    }

    with _refunds():
        result = services.cancel_trips(plan.lead, [a], user=_agent())

    a.refresh_from_db(), b.refresh_from_db(), plan.lead.refresh_from_db(), offer.refresh_from_db()
    assert result.refunded == Decimal("667.50") and result.cancelled == [a]
    assert result.order_cancelled is False
    assert (a.trip_status, a.revenue_status) == ("cancelled", "reversed")
    assert (b.trip_status, b.revenue_status) == (b.trip_status, "deferred")
    assert plan.lead.status == Lead.Status.BOOKED
    assert offer.status == Assignment.Status.WITHDRAWN and offer.note == "Trip cancelled"
    assert TouchPoint.objects.get(pk=queued[a.pk].pk).status == TouchPoint.Status.CANCELLED
    assert TouchPoint.objects.get(pk=queued[b.pk].pk).status == TouchPoint.Status.SCHEDULED
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("667.50")
    assert services.remaining_balance(plan.lead) == b.line_total - Decimal("667.50")


def test_cancelling_every_live_trip_loses_the_order_and_tells_the_customer():
    plan = _plan()
    a, b = _trip(plan.lead), _trip(plan.lead)
    notice = MessageRowFactory(
        trigger="order_cancelled", recipients=["booking_contact"], send_email=False
    )
    pickup = MessageRowFactory(trigger="pickup", recipients=["booking_contact"], send_email=False)
    per_trip = MessageRowFactory(
        trigger="trip_cancelled", recipients=["booking_contact"], send_email=False
    )
    stale = TouchPoint.objects.create(
        lead=plan.lead, reservation=b, message=pickup, kind=pickup.key, scheduled_for=timezone.now()
    )

    with _refunds(), patch.object(events, "_send_on_commit"):
        result = services.cancel_trips(plan.lead, [a, b], user=_agent())

    plan.lead.refresh_from_db()
    assert result.order_cancelled is True and result.refunded == Decimal("1335.00")
    assert plan.lead.status == Lead.Status.LOST and plan.lead.lost_reason == "Cancelled"
    assert TouchPoint.objects.get(pk=stale.pk).status == TouchPoint.Status.CANCELLED
    queued = TouchPoint.objects.filter(status=TouchPoint.Status.SCHEDULED)
    assert queued.filter(message=notice).count() == 1
    assert sorted(queued.filter(message=per_trip).values_list("reservation_id", flat=True)) == [
        a.pk,
        b.pk,
    ]


def test_a_trip_whose_pickup_has_passed_needs_an_admin():
    plan = _plan()
    gone, ahead = _trip(plan.lead, days=-1), _trip(plan.lead)

    with pytest.raises(services.PaymentError, match="Admin"), _refunds():
        services.cancel_trips(plan.lead, [gone], user=_agent())
    gone.refresh_from_db()
    assert gone.trip_status != Reservation.TripStatus.CANCELLED

    with _refunds():
        services.cancel_trips(plan.lead, [gone], user=_admin())
    gone.refresh_from_db()
    assert gone.trip_status == Reservation.TripStatus.CANCELLED
    assert services.cancel_options(plan.lead, _agent())[0]["past"] is False
    assert [o["pk"] for o in services.cancel_options(plan.lead, _agent())] == [ahead.pk]


def test_nothing_chosen_or_a_trip_off_the_order_is_refused():
    plan = _plan()
    _trip(plan.lead)
    with pytest.raises(services.PaymentError):
        services.cancel_trips(plan.lead, [], user=_admin())
    with pytest.raises(services.PaymentError):
        services.cancel_trips(plan.lead, [_trip(_plan().lead)], user=_admin())


def test_a_refund_stripe_refuses_leaves_the_trip_uncancelled():
    import stripe

    plan = _plan()
    a = _trip(plan.lead)
    with (
        patch(REFUND, side_effect=stripe.error.StripeError("no")),
        pytest.raises(stripe.error.StripeError),
    ):
        services.cancel_trips(plan.lead, [a], user=_admin())
    a.refresh_from_db()
    assert a.trip_status != Reservation.TripStatus.CANCELLED


# --- the endpoint and the page -------------------------------------------------------------


def test_the_endpoint_cancels_the_chosen_trips_and_answers_json(client):
    plan = _plan()
    a, b = _trip(plan.lead), _trip(plan.lead)
    client.force_login(_admin())
    with _refunds():
        resp = client.post(
            reverse("order_cancel_refund", args=[plan.lead_id]),
            {"trips": [a.pk]},
            HTTP_ACCEPT="application/json",
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "refunded": "667.50",
        "cancelled": [a.pk],
        "order_cancelled": False,
    }
    b.refresh_from_db()
    assert b.trip_status != Reservation.TripStatus.CANCELLED


def test_the_endpoint_refuses_an_agents_past_trip_with_the_reason(client):
    plan = _plan()
    gone = _trip(plan.lead, days=-1)
    client.force_login(_agent())
    resp = client.post(
        reverse("order_cancel_refund", args=[plan.lead_id]),
        {"trips": [gone.pk]},
        HTTP_ACCEPT="application/json",
    )
    assert resp.status_code == 400 and "Admin" in resp.json()["error"]


def test_a_plain_post_still_cancels_the_whole_order(client):
    plan = _plan()
    _trip(plan.lead), _trip(plan.lead)
    client.force_login(_admin())
    with _refunds():
        resp = client.post(reverse("order_cancel_refund", args=[plan.lead_id]))
    assert resp.status_code == 302
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.LOST


def test_the_order_page_offers_the_trips_to_cancel(client):
    plan = _plan()
    a = _trip(plan.lead)
    client.force_login(_admin())
    html = client.get(reverse("lead_detail", args=[plan.lead_id])).content.decode()
    assert "cancelRefund(" in html
    assert a.reference in html and "1335.00" in html
