import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_dashboard_requires_login(client):
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_dashboard_renders_for_authenticated_user(client, agent):
    client.force_login(agent)
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 200
    assert "portal/dashboard.html" in {t.name for t in resp.templates}


def test_dashboard_reports_pipeline_counts(client, agent):
    LeadFactory(status=Lead.Status.NEW)
    LeadFactory(status=Lead.Status.QUOTED)
    LeadFactory(status=Lead.Status.QUOTED)
    LeadFactory(status=Lead.Status.BOOKED)
    client.force_login(agent)
    resp = client.get(reverse("dashboard"))
    counts = resp.context["counts"]
    assert counts["new"] == 1
    assert counts["quoted"] == 2
    assert counts["booked"] == 1
