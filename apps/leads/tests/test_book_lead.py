"""Manual Mark booked: status, plan, touch-points, LA push — no payment recorded."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

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


def _book_button(html: str) -> str:
    """The Book now button's tag + attributes. Found from its icon+label, not a regex over
    the tag: the @click attribute holds an arrow function, so `>` appears inside it."""
    end = html.find("</i> Book now")
    assert end != -1, "no Book now button"
    return html[html.rfind("<button", 0, end) : end]


def test_lead_detail_shows_book_now_for_new_and_quoted(logged_in_client):
    for status in (Lead.Status.NEW, Lead.Status.QUOTED):
        lead = _quoted_lead(status=status)
        body = logged_in_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
        assert "Mark booked" not in body
        assert reverse("lead_mark_booked", args=[lead.pk]) in body
        assert 'title="Add a trip first"' not in _book_button(body)


def test_lead_detail_disables_book_now_without_trips(logged_in_client):
    lead = LeadFactory(status=Lead.Status.NEW)
    body = logged_in_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert 'disabled title="Add a trip first"' in _book_button(body)


def test_lead_detail_hides_book_now_once_booked(logged_in_client):
    lead = _quoted_lead(status=Lead.Status.BOOKED)
    body = logged_in_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Book now" not in body


def test_send_link_label_follows_whether_the_link_was_sent(logged_in_client):
    lead = _quoted_lead(status=Lead.Status.BOOKED)
    PaymentPlan.objects.create(lead=lead, quote_total=Decimal("500.00"))
    body = logged_in_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Send payment link" in body and "Resend payment link" not in body

    lead.quote_sent_at = timezone.now()
    lead.save(update_fields=["quote_sent_at"])
    body = logged_in_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Resend payment link" in body


def test_booking_banner_shows_only_with_the_flag_on_a_new_lead(logged_in_client):
    lead = LeadFactory(status=Lead.Status.NEW)
    url = reverse("lead_detail", args=[lead.pk])
    assert "Booking in progress" in logged_in_client.get(url, {"booking": "1"}).content.decode()
    assert "Booking in progress" not in logged_in_client.get(url).content.decode()
    booked = _quoted_lead(status=Lead.Status.BOOKED)
    booked_url = reverse("lead_detail", args=[booked.pk])
    assert (
        "Booking in progress"
        not in logged_in_client.get(booked_url, {"booking": "1"}).content.decode()
    )


def test_lead_detail_take_payment_replaces_offline_for_admins(client):
    from apps.accounts.factories import UserFactory
    from apps.accounts.models import User

    lead = _quoted_lead()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Take payment" in body
    assert "Charge card" in body
    assert "Mark paid (offline)" not in body
