"""APC-26 step 3 — a deposit hold that lapses, and the way back from it.

The client's Exception 1: the customer authorizes, APC doesn't answer inside the hold
window, the issuer releases the money. Nobody is at fault and no money moved, but the
order is now nothing — so the system has to notice, say so, and give the customer a live
link back with an honest explanation.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.notifications.models import Notification
from apps.payments import services, tasks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _intent(status, pi="pi_1"):
    return MagicMock(id=pi, status=status, amount=133500)


def _engaged(*, hours_left=72):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.status = Lead.Status.ENGAGED
    plan.lead.quote_expires_at = timezone.now() + timedelta(days=3)
    plan.lead.save(update_fields=["status", "quote_expires_at"])
    plan.deposit_status = PaymentPlan.DepositStatus.AUTHORIZED
    plan.save(update_fields=["deposit_status"])
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = "pi_1"
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = timezone.now() - timedelta(days=7) + timedelta(hours=hours_left)
    charge.capture_expires_at = timezone.now() + timedelta(hours=hours_left)
    charge.save()
    return plan, charge


# --- Stripe is the fact, our clock is only a prediction --------------------------------


def test_a_released_hold_expires_the_charge_and_revives_the_quote():
    plan, charge = _engaged(hours_left=-2)

    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        assert tasks.sweep_authorization_holds() == 1

    charge.refresh_from_db()
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert charge.status == Charge.Status.EXPIRED
    assert plan.deposit_status == PaymentPlan.DepositStatus.REQUESTED
    # back to a live quote they can act on, not lost
    assert plan.lead.status == Lead.Status.QUOTED
    assert plan.lead.quote_expired is False
    # no money ever moved, so the books say nothing
    assert not JournalEntry.objects.filter(lead=plan.lead).exists()


def test_a_hold_past_our_estimate_but_still_capturable_is_left_alone():
    """The issuer decides, not us. Expiring a capturable hold would throw away real money
    — our deadline only drives the warning."""
    plan, charge = _engaged(hours_left=-2)

    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_intent("requires_capture")
    ):
        tasks.sweep_authorization_holds()

    charge.refresh_from_db()
    plan.lead.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED
    assert plan.lead.status == Lead.Status.ENGAGED


def test_a_healthy_hold_well_inside_the_window_is_untouched():
    plan, charge = _engaged(hours_left=72)

    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_intent("requires_capture")
    ):
        assert tasks.sweep_authorization_holds() == 0

    charge.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED
    assert not Notification.objects.filter(lead=plan.lead).exists()


def test_a_hold_nearing_expiry_warns_staff_once():
    plan, _ = _engaged(hours_left=6)

    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_intent("requires_capture")
    ):
        tasks.sweep_authorization_holds()
        tasks.sweep_authorization_holds()  # a second tick must not re-nag

    assert (
        Notification.objects.filter(lead=plan.lead, kind=Notification.Kind.AUTH_EXPIRING).count()
        == 1
    )


def test_a_hold_captured_outside_the_app_is_booked_not_expired():
    """`succeeded` means the money HAS moved — capturing from the Stripe dashboard, or a
    `confirm_order` whose local save lost the race after the capture went through. Treating
    it as a lapse would post no ledger entry and tell the customer nothing was taken."""
    plan, charge = _engaged(hours_left=-2)
    captured = MagicMock(
        id="pi_1",
        status="succeeded",
        amount=133500,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )
    with (
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=captured),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        tasks.sweep_authorization_holds()

    charge.refresh_from_db()
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED
    assert plan.lead.status == Lead.Status.BOOKED
    assert JournalEntry.objects.filter(lead=plan.lead).exists()
    assert not TouchPoint.objects.filter(
        lead=plan.lead, kind=TouchPoint.Kind.ORDER_AUTH_EXPIRED
    ).exists()


def test_a_second_hold_after_a_lapse_warns_staff_again():
    """The dedupe is per hold, not per lead — a customer who re-authorizes and runs the
    clock down a second time still needs someone to act on it."""
    plan, first = _engaged(hours_left=6)
    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_intent("requires_capture")
    ):
        tasks.sweep_authorization_holds()
    first.status = Charge.Status.EXPIRED
    first.save(update_fields=["status"])

    second = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    second.stripe_payment_intent_id = "pi_2"
    second.status = Charge.Status.AUTHORIZED
    second.authorized_at = timezone.now()
    second.capture_expires_at = timezone.now() + timedelta(hours=6)
    second.save()

    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_intent("requires_capture", "pi_2")
    ):
        tasks.sweep_authorization_holds()

    assert (
        Notification.objects.filter(lead=plan.lead, kind=Notification.Kind.AUTH_EXPIRING).count()
        == 2
    )


def test_an_expired_hold_notifies_staff():
    plan, _ = _engaged(hours_left=-2)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        tasks.sweep_authorization_holds()

    assert Notification.objects.filter(lead=plan.lead, kind=Notification.Kind.AUTH_EXPIRED).exists()


def test_the_sweep_runs_inside_the_hourly_reconcile_job():
    """No new cron entry — three are already waiting to be registered."""
    from apps.core.cron import JOBS

    assert JOBS["reconcile-payments"] is tasks.reconcile_payments


# --- telling the customer -------------------------------------------------------------


def test_expiry_queues_the_customer_explanation():
    plan, _ = _engaged(hours_left=-2)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        tasks.sweep_authorization_holds()

    tp = TouchPoint.objects.get(lead=plan.lead, kind=TouchPoint.Kind.ORDER_AUTH_EXPIRED)
    assert tp.status == TouchPoint.Status.SCHEDULED
    assert tp.scheduled_for <= timezone.now()


def test_the_explanation_is_never_sent_without_a_working_link(settings):
    """The whole message is the link back — with PUBLIC_BASE_URL unset `pay_link` renders
    as a bare path, and a SENT row would never be retried. Stay SCHEDULED instead."""
    from apps.messaging import touchpoints as messaging_touchpoints

    settings.PUBLIC_BASE_URL = ""
    plan, _ = _engaged(hours_left=-2)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        tasks.sweep_authorization_holds()

    tp = TouchPoint.objects.get(lead=plan.lead, kind=TouchPoint.Kind.ORDER_AUTH_EXPIRED)
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert messaging_touchpoints._process(tp) is False
    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED


# --- the way back --------------------------------------------------------------------


def test_the_pay_page_works_again_and_explains_the_released_hold(client):
    plan, _ = _engaged(hours_left=-2)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        tasks.sweep_authorization_holds()

    from apps.leads.services import make_deposit_token

    plan.lead.refresh_from_db()
    resp = client.get(reverse("quote_pay", args=[make_deposit_token(plan.lead)]))

    body = resp.content.decode()
    assert resp.status_code == 200
    assert resp.context["pay_kind"] == Charge.Kind.DEPOSIT  # payable again
    assert "earlier authorization was released" in body
    assert "no money was taken" in body


def test_a_normal_unpaid_quote_shows_no_released_hold_note(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.status = Lead.Status.QUOTED
    plan.lead.quote_expires_at = timezone.now() + timedelta(days=3)
    plan.lead.save(update_fields=["status", "quote_expires_at"])

    from apps.leads.services import make_deposit_token

    resp = client.get(reverse("quote_pay", args=[make_deposit_token(plan.lead)]))

    assert "earlier authorization was released" not in resp.content.decode()


def test_re_authorizing_after_an_expiry_engages_the_order_again():
    """The whole point of the way back: the second hold behaves like the first."""
    plan, _ = _engaged(hours_left=-2)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_intent("canceled")):
        tasks.sweep_authorization_holds()

    plan.refresh_from_db()
    fresh = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    fresh.stripe_payment_intent_id = "pi_2"
    fresh.save(update_fields=["stripe_payment_intent_id"])
    authorized = MagicMock(
        id="pi_2",
        status="requires_capture",
        amount=133500,
        payment_method=MagicMock(id="pm_2", card=MagicMock(brand="visa", last4="4242")),
    )
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=authorized):
        services.record_authorization(plan, "pi_2")

    plan.lead.refresh_from_db()
    fresh.refresh_from_db()
    assert plan.lead.status == Lead.Status.ENGAGED
    assert fresh.status == Charge.Status.AUTHORIZED
