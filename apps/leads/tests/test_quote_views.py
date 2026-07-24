"""Quote-page view tracking — count every open, fire touch-points once."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def quoted_lead():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead, rate="500.00")
    return lead


def test_every_open_increments_the_count(client, quoted_lead):
    url = reverse("quote_page", args=[services.make_deposit_token(quoted_lead)])
    for _ in range(3):
        assert client.get(url).status_code == 200
    quoted_lead.refresh_from_db()
    assert quoted_lead.quote_view_count == 3
    assert quoted_lead.quote_last_viewed_at is not None


def test_touchpoints_fire_only_on_the_first_open(client, quoted_lead):
    url = reverse("quote_page", args=[services.make_deposit_token(quoted_lead)])
    with patch("apps.leads.views.touchpoints.schedule_quote_viewed") as scheduled:
        for _ in range(3):
            client.get(url)
    assert scheduled.call_count == 1


def test_first_viewed_at_is_stamped_once(client, quoted_lead):
    url = reverse("quote_page", args=[services.make_deposit_token(quoted_lead)])
    client.get(url)
    quoted_lead.refresh_from_db()
    first = quoted_lead.quote_viewed_at
    client.get(url)
    quoted_lead.refresh_from_db()
    assert quoted_lead.quote_viewed_at == first
    assert quoted_lead.quote_last_viewed_at > first


def test_billing_contact_falls_back_to_the_primary(quoted_lead):
    assert quoted_lead.effective_billing_contact == quoted_lead.contact


def test_billing_contact_is_used_when_set():
    from apps.contacts.factories import ContactFactory

    billing = ContactFactory(name="Accounts Payable")
    lead = LeadFactory(billing_contact=billing)
    assert lead.effective_billing_contact == billing
