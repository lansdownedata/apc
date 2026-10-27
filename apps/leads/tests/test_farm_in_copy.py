"""The affiliate-account copy states the effective farm-in clock, not a hard-coded one."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.tasks.models import TaskSchedule

pytestmark = pytest.mark.django_db


def _pages(client):
    client.force_login(UserFactory())
    company = CompanyFactory(is_affiliate_account=True)
    lead = LeadFactory(contact=ContactFactory(company=company))
    return [
        client.get(reverse("lead_detail", args=[lead.pk])).content.decode(),
        client.get(reverse("company_detail", args=[company.pk])).content.decode(),
    ]


def test_default_clock_is_stated(client):
    for page in _pages(client):
        assert "deposit due 3 days before the trip, balance 24 hours after" in page


def test_a_saved_farm_in_row_changes_the_copy(client):
    TaskSchedule.objects.create(kind="deposit_received", variant="farm_in", amount=5, unit="days")
    for page in _pages(client):
        assert "deposit due 5 days before the trip" in page
