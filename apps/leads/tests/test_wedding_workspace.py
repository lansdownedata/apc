"""The wedding builder's portal surface: the save route, the intent, the workspace."""

import json

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.public.tests.test_wedding_form import _legs, _post

pytestmark = pytest.mark.django_db


def _portal(**over):
    data = _post(**over)
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)
    return data


@pytest.fixture
def agent(client):
    user = UserFactory()
    client.force_login(user)
    return user


def test_the_save_route_requires_login(client):
    lead = LeadFactory()
    resp = client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    assert resp.status_code == 302
    assert "/portal/login/" in resp["Location"]


def test_a_valid_save_builds_the_trips_and_returns_to_the_workspace(client, agent):
    lead = LeadFactory()
    resp = client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    assert resp.status_code == 302
    assert resp["Location"] == reverse("lead_detail", args=[lead.pk])
    assert lead.reservations.count() == 2


def test_an_invalid_save_creates_nothing(client, agent):
    lead = LeadFactory()
    client.post(
        reverse("lead_wedding_save", args=[lead.pk]),
        _portal(legs_json=json.dumps(_legs(13))),
    )
    assert lead.reservations.count() == 0


def test_the_query_string_opens_the_builder(client, agent):
    lead = LeadFactory()
    resp = client.get(f"{reverse('lead_detail', args=[lead.pk])}?wedding=1")
    assert resp.context["wedding_open"] is True


def test_a_lead_with_a_saved_plan_offers_the_builder_without_the_query_string(client, agent):
    lead = LeadFactory()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    resp = client.get(reverse("lead_detail", args=[lead.pk]))
    assert resp.context["wedding_state"] is not None
    assert resp.context["wedding_open"] is False


def test_an_ordinary_lead_has_no_wedding_state(client, agent):
    resp = client.get(reverse("lead_detail", args=[LeadFactory().pk]))
    assert resp.context["wedding_state"] is None


def test_the_saved_state_carries_the_vehicle_the_agent_assigned(client, agent):
    """Reopening the builder shows what was chosen, not the recommendation again."""
    from apps.leads.factories import VehicleTypeFactory

    lead = LeadFactory()
    vehicle = VehicleTypeFactory(name="Wedding Coach", capacity=56)
    client.post(
        reverse("lead_wedding_save", args=[lead.pk]),
        _portal(vehicles_json=json.dumps({"guests-in": vehicle.pk})),
    )
    state = client.get(reverse("lead_detail", args=[lead.pk])).context["wedding_state"]
    legs = {leg["id"]: leg for leg in state["legs"]}
    assert legs["guests-in"]["vehicle_id"] == vehicle.pk
    assert legs["final-out"]["vehicle_id"] is None


def test_the_workspace_summarises_the_day(client, agent):
    lead = LeadFactory()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "The Oak Barn at Loyalty" in body
    assert "Edit the day" in body


def test_orphaned_trips_are_reported_to_the_agent(client, agent):
    lead = LeadFactory()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    resp = client.post(
        reverse("lead_wedding_save", args=[lead.pk]),
        _portal(legs_json=json.dumps(_legs()[:1])),
        follow=True,
    )
    assert "no longer in the plan" in resp.content.decode().lower()


def test_new_wedding_creates_a_lead_and_opens_the_builder(client, agent):
    resp = client.post(
        reverse("lead_create"),
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "channel": "website",
            "intent": "wedding",
        },
    )
    lead = Lead.objects.get()
    assert resp["Location"] == f"{reverse('lead_detail', args=[lead.pk])}?wedding=1"


def test_new_wedding_schedules_no_touch_points(client, agent, monkeypatch):
    """The website-worded TP1/TP2 copy is wrong for a wedding taken by phone."""
    called = []
    monkeypatch.setattr(
        "apps.leads.views.touchpoints.schedule_lead_created", lambda lead: called.append(lead)
    )
    client.post(
        reverse("lead_create"),
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "channel": "phone",
            "intent": "wedding",
        },
    )
    assert called == []


def test_an_ordinary_new_lead_still_schedules_touch_points(client, agent, monkeypatch):
    called = []
    monkeypatch.setattr(
        "apps.leads.views.touchpoints.schedule_lead_created", lambda lead: called.append(lead)
    )
    client.post(
        reverse("lead_create"),
        {"name": "Jane Rider", "email": "jane@example.com", "channel": "website"},
    )
    assert len(called) == 1
