"""Manual Mark booked: status, plan, touch-points, LA push — no payment recorded."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.payments.models import PaymentPlan
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db

JSON_HEADERS = {"HTTP_ACCEPT": "application/json"}


def _quoted_lead(**kwargs):
    kwargs.setdefault("status", Lead.Status.QUOTED)
    lead = LeadFactory(**kwargs)
    TransferReservationFactory(lead=lead, rate=Decimal("500.00"))
    return lead


def test_book_lead_sets_booked_and_creates_plan():
    lead = _quoted_lead()
    with patch("apps.integrations.la_sync.push_lead_bookings") as push:
        services.book_lead(lead)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED
    plan = PaymentPlan.objects.get(lead=lead)
    assert plan.quote_total == Decimal("500.00")
    push.assert_called_once()
    assert push.call_args.args[0].pk == lead.pk


def test_book_lead_is_idempotent_when_already_booked():
    lead = _quoted_lead(status=Lead.Status.BOOKED)
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        services.book_lead(lead)
        services.book_lead(lead)
    assert PaymentPlan.objects.filter(lead=lead).count() == 1
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_book_lead_refuses_lost():
    lead = LeadFactory(status=Lead.Status.LOST)
    with pytest.raises(services.BookLeadError):
        services.book_lead(lead)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.LOST


def test_book_lead_cancels_pending_touchpoints():
    lead = _quoted_lead()
    TouchPoint.objects.create(
        lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS, status=TouchPoint.Status.SCHEDULED
    )
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        services.book_lead(lead)
    assert not TouchPoint.objects.filter(lead=lead, status=TouchPoint.Status.SCHEDULED).exists()


def test_book_lead_la_failure_still_books():
    lead = _quoted_lead()
    with patch("apps.integrations.la_sync.push_lead_bookings", side_effect=RuntimeError("boom")):
        services.book_lead(lead)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_mark_booked_endpoint_books_quoted(logged_in_client):
    lead = _quoted_lead()
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        resp = logged_in_client.post(reverse("lead_mark_booked", args=[lead.pk]), **JSON_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_mark_booked_books_a_new_lead_with_trips(logged_in_client):
    """Phone bookings: no quote email, no payment — straight to Booked."""
    lead = _quoted_lead(status=Lead.Status.NEW)
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        resp = logged_in_client.post(reverse("lead_mark_booked", args=[lead.pk]), **JSON_HEADERS)
    assert resp.status_code == 200
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_mark_booked_refuses_a_new_lead_with_no_trips(logged_in_client):
    lead = LeadFactory(status=Lead.Status.NEW)
    resp = logged_in_client.post(reverse("lead_mark_booked", args=[lead.pk]), **JSON_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"] == "Add at least one trip before booking."
    lead.refresh_from_db()
    assert lead.status == Lead.Status.NEW


def test_mark_booked_refuses_lost(logged_in_client):
    lead = LeadFactory(status=Lead.Status.LOST)
    resp = logged_in_client.post(reverse("lead_mark_booked", args=[lead.pk]), **JSON_HEADERS)
    assert resp.status_code == 400
    assert "new or quoted" in resp.json()["error"]


def test_mark_booked_requires_login(client):
    lead = _quoted_lead()
    resp = client.post(reverse("lead_mark_booked", args=[lead.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_book_lead_from_new_creates_an_unsent_deposit_plan():
    lead = _quoted_lead(status=Lead.Status.NEW)
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        services.book_lead(lead)
    plan = PaymentPlan.objects.get(lead=lead)
    assert plan.deposit_status == PaymentPlan.DepositStatus.UNSENT
    assert plan.quote_total == Decimal("500.00")


def test_lead_detail_shows_mark_booked_for_quoted(logged_in_client):
    lead = _quoted_lead()
    resp = logged_in_client.get(reverse("lead_detail", args=[lead.pk]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Mark booked" in body
    assert reverse("lead_mark_booked", args=[lead.pk]) in body
    assert "Remaining" in body


def test_lead_detail_take_payment_replaces_offline_for_admins(client):
    from apps.accounts.factories import UserFactory
    from apps.accounts.models import User

    lead = _quoted_lead()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Take payment" in body
    assert "Charge card" in body
    assert "Mark paid (offline)" not in body
