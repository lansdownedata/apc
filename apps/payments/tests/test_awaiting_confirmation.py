"""APC-26 step 2 — the awaiting-confirmation queue and its staff actions.

Engaged orders are a time-boxed worklist: money is on hold, a customer is waiting, and the
authorization lapses on the issuer's clock. The queue leads with what expires soonest.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.models import Lead
from apps.payments import reports, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def money_client(client):
    """Confirm/cancel move real money, so both are behind payment access."""
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    return client


def _engaged(*, hours_left=72, total="2670.00", pickup_in_days=20):
    plan = PaymentPlanFactory(quote_total=Decimal(total))
    plan.lead.status = Lead.Status.ENGAGED
    plan.lead.save(update_fields=["status"])
    ReservationFactory(
        lead=plan.lead, pickup_date=(timezone.now() + timedelta(days=pickup_in_days)).date()
    )
    plan.deposit_status = plan.DepositStatus.AUTHORIZED
    plan.save(update_fields=["deposit_status"])
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = f"pi_{charge.pk}"
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = timezone.now()
    charge.capture_expires_at = timezone.now() + timedelta(hours=hours_left)
    charge.save()
    return plan


# --- the queue ----------------------------------------------------------------------


def test_queue_lists_engaged_orders_with_what_is_held_and_when_it_lapses():
    plan = _engaged(hours_left=30, total="2670.00")

    rows = reports.awaiting_confirmation_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row["lead"] == plan.lead
    assert row["held"] == plan.deposit_amount
    assert 29 <= row["hours_left"] <= 30


def test_queue_is_sorted_by_what_expires_soonest():
    later = _engaged(hours_left=70)
    sooner = _engaged(hours_left=5)

    rows = reports.awaiting_confirmation_rows()

    assert [r["lead"] for r in rows] == [sooner.lead, later.lead]


@pytest.mark.parametrize(
    ("hours_left", "tier"),
    [(72, ""), (30, "warning"), (6, "critical"), (-1, "critical")],
)
def test_urgency_tiers_track_the_hold_deadline(hours_left, tier):
    _engaged(hours_left=hours_left)
    assert reports.awaiting_confirmation_rows()[0]["tier"] == tier


def test_a_booked_order_is_not_awaiting_anything():
    plan = _engaged()
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status"])

    assert reports.awaiting_confirmation_rows() == []


def test_summary_totals_the_money_on_hold():
    _engaged(total="2670.00")
    _engaged(total="1000.00")

    summary = reports.awaiting_confirmation_summary()

    assert summary["count"] == 2
    assert summary["held"] == Decimal("1835.00")  # 50% of each
    assert summary["soonest_hours"] is not None


def test_summary_is_empty_with_nothing_engaged():
    assert reports.awaiting_confirmation_summary() == {
        "count": 0,
        "held": Decimal("0.00"),
        "soonest_hours": None,
        "tier": "",
    }


# --- the console ---------------------------------------------------------------------


def test_orders_console_shows_the_queue_filter_with_a_count(money_client):
    _engaged()
    resp = money_client.get(reverse("orders_list"))
    assert b"Awaiting confirmation" in resp.content


def test_awaiting_filter_lists_only_engaged_orders(money_client):
    engaged = _engaged()
    booked = PaymentPlanFactory()
    booked.lead.status = Lead.Status.BOOKED
    booked.lead.save(update_fields=["status"])

    resp = money_client.get(reverse("orders_list"), {"filter": "awaiting"})

    body = resp.content.decode()
    assert engaged.lead.quote_no in body
    assert booked.lead.quote_no not in body


# --- confirm / cancel ----------------------------------------------------------------


def test_confirm_endpoint_captures_and_books(money_client):
    plan = _engaged()
    captured = MagicMock(
        id="pi_1",
        status="succeeded",
        amount=133500,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )
    with (
        patch.object(services.stripe.PaymentIntent, "capture"),
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=captured),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        resp = money_client.post(reverse("order_confirm", args=[plan.lead_id]))

    assert resp.status_code in (200, 302)
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED


def test_cancel_endpoint_releases_the_hold_with_a_reason(money_client):
    plan = _engaged()
    with patch.object(services.stripe.PaymentIntent, "cancel"):
        resp = money_client.post(
            reverse("order_cancel", args=[plan.lead_id]), {"reason": "No coach available"}
        )

    assert resp.status_code in (200, 302)
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.LOST
    assert plan.lead.lost_reason == "No coach available"


def test_confirm_refuses_an_order_with_no_authorization(money_client):
    plan = PaymentPlanFactory()
    resp = money_client.post(reverse("order_confirm", args=[plan.lead_id]))
    assert resp.status_code == 400


def test_cancel_refuses_an_order_that_is_no_longer_engaged(money_client):
    """A captured order is cancelled through the refund path — releasing a hold must never
    be the way a paid booking gets marked lost with the money still taken."""
    plan = _engaged()
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status"])

    with patch.object(services.stripe.PaymentIntent, "cancel") as cancel:
        resp = money_client.post(reverse("order_cancel", args=[plan.lead_id]))

    assert resp.status_code == 400
    cancel.assert_not_called()
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED


def test_confirm_reports_a_stripe_failure_instead_of_erroring(money_client):
    """The hold can lapse between the render and the click — the queue still shows Confirm
    on a lapsed row, so Stripe's refusal has to come back as a readable message."""
    plan = _engaged(hours_left=-1)
    boom = services.stripe.error.InvalidRequestError(
        "This PaymentIntent could not be captured.", None
    )
    with patch.object(services.stripe.PaymentIntent, "capture", side_effect=boom):
        resp = money_client.post(reverse("order_confirm", args=[plan.lead_id]))

    assert resp.status_code == 502
    assert resp.json()["ok"] is False
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.ENGAGED


def test_the_queue_does_not_count_trips_one_order_at_a_time(django_assert_num_queries):
    """The rows render on the dashboard and the console — a COUNT per engaged order is a
    per-row query that grows with the queue."""
    for _ in range(4):
        _engaged()

    with django_assert_num_queries(2):  # leads (+ annotations) and the prefetched charges
        assert len(reports.awaiting_confirmation_rows()) == 4


def test_a_user_without_payment_access_is_not_offered_the_decisions(client):
    _engaged()
    client.force_login(UserFactory())  # agent, no payments access

    body = client.get(reverse("orders_list"), {"filter": "awaiting"}).content.decode()

    assert "confirmOrder(" not in body
    assert "Payments access required" in body


# --- the dashboard tile --------------------------------------------------------------


def test_dashboard_surfaces_the_queue(money_client):
    _engaged(hours_left=9)
    resp = money_client.get(reverse("dashboard"))
    assert resp.context["awaiting"]["count"] == 1
    assert b"awaiting confirmation" in resp.content.lower()


# --- the quote workspace -------------------------------------------------------------


def test_workspace_offers_the_two_decisions_on_an_engaged_order(money_client):
    plan = _engaged(hours_left=30)
    resp = money_client.get(reverse("lead_detail", args=[plan.lead_id]))
    body = resp.content.decode()
    assert "Confirm &amp; capture" in body
    assert "Cancel order" in body
    assert resp.context["authorized_held"] == plan.deposit_amount
    assert resp.context["authorized_tier"] == "warning"


def test_workspace_hides_the_decisions_on_a_booked_order(money_client):
    plan = _engaged()
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status"])
    resp = money_client.get(reverse("lead_detail", args=[plan.lead_id]))
    assert "Confirm &amp; capture" not in resp.content.decode()
    assert resp.context["authorized_held"] is None


def test_a_user_without_payment_access_cannot_confirm(client):
    plan = _engaged()
    client.force_login(UserFactory())  # agent, no payments access
    resp = client.post(reverse("order_confirm", args=[plan.lead_id]))
    assert resp.status_code in (302, 403)
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.ENGAGED
