"""New booking entry point: the contact modal with intent=booking lands on the workspace
flagged as a booking and skips the website-worded welcome touch-points."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads import services
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _contact(**over):
    data = {"name": "Phone Booker", "phone": "+12025550100", "channel": "phone"}
    data.update(over)
    return data


def test_booking_intent_flags_the_workspace_and_skips_welcome_touchpoints(staff):
    resp = staff.post(reverse("lead_create"), _contact(intent="booking"))
    lead = Lead.objects.latest("id")
    assert resp.status_code == 302
    assert resp.url == reverse("lead_detail", args=[lead.pk]) + "?booking=1"
    assert lead.status == Lead.Status.NEW
    assert not TouchPoint.objects.filter(lead=lead).exists()


def test_a_plain_new_lead_still_gets_tp1_and_tp2(staff):
    staff.post(reverse("lead_create"), _contact())
    lead = Lead.objects.latest("id")
    kinds = set(TouchPoint.objects.filter(lead=lead).values_list("kind", flat=True))
    assert kinds == {TouchPoint.Kind.TP1_WELCOME, TouchPoint.Kind.TP2_LEAD_FOLLOWUP}


def test_leads_list_offers_new_booking(staff):
    html = staff.get(reverse("lead_list")).content.decode()
    assert "New booking" in html
    assert 'name="intent"' in html
    assert "newLeadIntent" in html


def test_orders_console_offers_new_booking(staff):
    html = staff.get(reverse("orders_list")).content.decode()
    assert "New booking" in html
    assert 'name="intent"' in html
    assert 'id="nl-agent"' in html  # the modal's agent picker is fed on this page too


def test_agent_options_lists_users_by_name():
    UserFactory(username="zed", first_name="Zed", last_name="Last")
    UserFactory(username="amy", first_name="Amy", last_name="First")
    labels = [label for _, label in services.agent_options()]
    assert labels.index("Amy First") < labels.index("Zed Last")
