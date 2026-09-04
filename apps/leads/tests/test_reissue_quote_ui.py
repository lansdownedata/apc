"""APC-25 — the quote workspace's Reissue button + expiry help text."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())
    return client


def _quote(*, expires_in_days):
    lead = LeadFactory(
        status=Lead.Status.QUOTED,
        contact=ContactFactory(email="r@example.com"),
        quote_sent_at=timezone.now() - timedelta(days=20),
        quote_expires_at=timezone.now() + timedelta(days=expires_in_days),
    )
    TransferReservationFactory(lead=lead)
    return lead


def _html(client, lead):
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


def test_expired_quote_shows_a_reissue_button(agent):
    lead = _quote(expires_in_days=-3)

    html = _html(agent, lead)

    assert reverse("lead_reissue_quote", args=[lead.pk]) in html
    assert "Reissue quote" in html


def test_an_active_quote_has_no_reissue_button(agent):
    lead = _quote(expires_in_days=10)

    html = _html(agent, lead)

    assert reverse("lead_reissue_quote", args=[lead.pk]) not in html


def test_the_expiry_date_shows_as_help_text(agent):
    lead = _quote(expires_in_days=10)

    html = _html(agent, lead)

    assert "Quote expires" in html
    assert lead.quote_expires_at.strftime("%b") in html


def test_an_expired_quote_says_expired_in_the_help_text(agent):
    lead = _quote(expires_in_days=-3)

    html = _html(agent, lead)

    assert "Quote expired" in html
