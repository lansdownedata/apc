"""Task 6: public quote page — view tracking, expiry states, T&Cs, book-now checkout."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _quoted_lead(**kwargs):
    kwargs.setdefault("status", Lead.Status.QUOTED)
    kwargs.setdefault("contact", ContactFactory(email="rider@example.com"))
    kwargs.setdefault("quote_expires_at", timezone.now() + timezone.timedelta(days=10))
    lead = LeadFactory(**kwargs)
    TransferReservationFactory(lead=lead, base_rate=Decimal("185.00"))
    PaymentPlanFactory(lead=lead, quote_total=Decimal("185.00"), deposit_pct=50)
    return lead


def test_quote_page_rejects_bad_token(client):
    resp = client.get(reverse("quote_page", args=["not-a-real-token"]))
    assert resp.status_code == 404


def test_quote_page_renders_quote_summary(client):
    lead = _quoted_lead()
    reservation = lead.reservations.first()
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert lead.quote_no in body
    assert reservation.service in body
    assert "50% deposit" in body
    assert "92.50" in body  # deposit amount
    assert "Terms &amp; Conditions" in body or "Terms & Conditions" in body


def test_quote_page_new_status_404s(client):
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email="a@b.com"))
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 404


def test_quote_page_lost_status_404s(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.LOST
    lead.save(update_fields=["status"])
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 404


def test_quote_page_first_view_stamps_and_schedules(client):
    lead = _quoted_lead()
    assert lead.quote_viewed_at is None
    token = services.make_deposit_token(lead)
    with patch.object(services.touchpoints, "schedule_quote_viewed") as scheduled:
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    assert lead.quote_viewed_at is not None
    scheduled.assert_called_once_with(lead)


def test_quote_page_second_view_does_not_restamp(client):
    lead = _quoted_lead()
    token = services.make_deposit_token(lead)
    with patch.object(services.touchpoints, "schedule_quote_viewed"):
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    first_stamp = lead.quote_viewed_at

    with patch.object(services.touchpoints, "schedule_quote_viewed") as scheduled:
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    assert lead.quote_viewed_at == first_stamp
    scheduled.assert_not_called()


def test_quote_page_expired_has_no_book_button_and_notifies_once(client):
    lead = _quoted_lead(quote_expires_at=timezone.now() - timezone.timedelta(days=1))
    token = services.make_deposit_token(lead)

    resp1 = client.get(reverse("quote_page", args=[token]))
    assert "expired" in resp1.content.decode().lower()
    assert "Book Now" not in resp1.content.decode()

    resp2 = client.get(reverse("quote_page", args=[token]))
    assert "expired" in resp2.content.decode().lower()

    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.QUOTE_EXPIRED).count() == 1


def test_quote_page_booked_shows_already_booked_note(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 200
    assert "already booked" in resp.content.decode().lower()
    assert "Book Now" not in resp.content.decode()


def test_quote_book_posts_to_stripe_and_redirects(client):
    lead = _quoted_lead()
    token = services.make_deposit_token(lead)
    with patch(
        "apps.leads.views.payment_services.create_deposit_checkout",
        return_value="https://stripe.test/sess",
    ) as create_checkout:
        resp = client.post(reverse("quote_book", args=[token]))
    assert resp.status_code == 302
    assert resp.url == "https://stripe.test/sess"
    assert create_checkout.call_count == 1
    kwargs = create_checkout.call_args.kwargs
    assert reverse("quote_deposit_success", args=[token]) in kwargs["success_url"]
    assert reverse("quote_deposit_cancel", args=[token]) in kwargs["cancel_url"]


def test_quote_book_get_not_allowed(client):
    lead = _quoted_lead()
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_book", args=[token]))
    assert resp.status_code == 405


def test_quote_book_expired_redirects_without_stripe_call(client):
    lead = _quoted_lead(quote_expires_at=timezone.now() - timezone.timedelta(days=1))
    token = services.make_deposit_token(lead)
    with patch("apps.leads.views.payment_services.create_deposit_checkout") as create_checkout:
        resp = client.post(reverse("quote_book", args=[token]))
    assert resp.status_code == 302
    assert resp.url == reverse("quote_page", args=[token])
    create_checkout.assert_not_called()


def test_quote_book_booked_redirects_without_stripe_call(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    token = services.make_deposit_token(lead)
    with patch("apps.leads.views.payment_services.create_deposit_checkout") as create_checkout:
        resp = client.post(reverse("quote_book", args=[token]))
    assert resp.status_code == 302
    assert resp.url == reverse("quote_page", args=[token])
    create_checkout.assert_not_called()


def test_quote_book_stripe_error_rerenders_with_message(client):
    import stripe

    lead = _quoted_lead()
    token = services.make_deposit_token(lead)
    with patch(
        "apps.leads.views.payment_services.create_deposit_checkout",
        side_effect=stripe.error.StripeError("Your card was declined."),
    ):
        resp = client.post(reverse("quote_book", args=[token]))
    assert resp.status_code == 200
    assert "Your card was declined." in resp.content.decode()


def test_quote_book_bad_token_404s(client):
    resp = client.post(reverse("quote_book", args=["not-a-real-token"]))
    assert resp.status_code == 404
