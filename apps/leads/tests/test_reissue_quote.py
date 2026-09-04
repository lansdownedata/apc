"""APC-25 — reissue an expired quote (a distinct action from Send / Resend)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _expired_quote():
    lead = LeadFactory(
        status=Lead.Status.QUOTED,
        contact=ContactFactory(email="rider@example.com"),
        quote_sent_at=timezone.now() - timedelta(days=30),
        quote_expires_at=timezone.now() - timedelta(days=2),
        quote_viewed_at=timezone.now() - timedelta(days=20),
    )
    TransferReservationFactory(lead=lead)
    return lead


# --- service -------------------------------------------------------------------------


def test_reissue_gives_the_quote_a_fresh_future_expiry():
    lead = _expired_quote()

    services.reissue_quote(lead)

    lead.refresh_from_db()
    assert lead.quote_expires_at > timezone.now()
    assert not lead.quote_expired


def test_reissue_resets_the_viewed_flag():
    lead = _expired_quote()

    services.reissue_quote(lead)

    lead.refresh_from_db()
    assert lead.quote_viewed_at is None


def test_reissue_does_not_email_or_text_the_customer():
    lead = _expired_quote()
    mail.outbox.clear()

    with pytest.MonkeyPatch().context() as mp:
        calls = []
        mp.setattr("apps.integrations.podium.send_message", lambda **kw: calls.append(kw))
        services.reissue_quote(lead)

    assert mail.outbox == []
    assert calls == []


def test_reissue_reschedules_the_quote_touch_points():
    lead = _expired_quote()

    with pytest.MonkeyPatch().context() as mp:
        seen = []
        mp.setattr(
            "apps.leads.services.touchpoints.schedule_quote_sent", lambda lead: seen.append(lead)
        )
        services.reissue_quote(lead)

    assert seen == [lead]


def test_reissue_keeps_the_original_sent_timestamp():
    lead = _expired_quote()
    original = lead.quote_sent_at

    services.reissue_quote(lead)

    lead.refresh_from_db()
    assert lead.quote_sent_at == original


def test_reissue_refuses_a_lead_that_was_never_quoted():
    lead = LeadFactory(status=Lead.Status.NEW)

    with pytest.raises(services.ReissueQuoteError):
        services.reissue_quote(lead)


# --- view ---------------------------------------------------------------------------


def _post(client, lead):
    return client.post(reverse("lead_reissue_quote", args=[lead.pk]))


def test_view_reissues_and_redirects_to_the_workspace(client):
    lead = _expired_quote()
    client.force_login(UserFactory())

    resp = _post(client, lead)

    assert resp.status_code == 302
    assert resp.url == reverse("lead_detail", args=[lead.pk])
    lead.refresh_from_db()
    assert not lead.quote_expired


def test_view_requires_login(client):
    lead = _expired_quote()

    resp = _post(client, lead)

    assert resp.status_code == 302
    assert "/login" in resp.url


def test_view_refuses_a_booked_lead(client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    client.force_login(UserFactory())

    resp = _post(client, lead)

    assert resp.status_code in (302, 400)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED
