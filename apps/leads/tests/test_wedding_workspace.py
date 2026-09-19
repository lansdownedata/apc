"""The wedding's portal surface: the save route, the New wedding intent, the workspace.
The details card and Edit details themselves are in test_wedding_details_editor.py."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.public.tests.test_wedding_form import _post

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


# --- the markup itself --------------------------------------------------------------


def test_new_wedding_appears_on_the_leads_and_orders_lists(client, agent):
    for url in (reverse("lead_list"), reverse("orders_list")):
        assert "New wedding" in client.get(url).content.decode()


def test_a_trip_row_reads_a_stop_by_name_when_it_has_no_street_address(client, agent):
    """A generated stop carries its meaning in `name` — "2 hotels — Hampton Inn, …" has
    no street address of its own, and the row rendered a bare dash for it."""
    from apps.reservations.factories import ReservationFactory
    from apps.reservations.models import Stop

    lead = LeadFactory()
    res = ReservationFactory(lead=lead)
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, name="2 hotels — Hampton Inn", address="")
    Stop.objects.create(reservation=res, sequence=1, name="The Oak Barn", address="")

    route = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "2 hotels — Hampton Inn" in route
    assert "The Oak Barn" in route


def test_the_office_and_the_customer_share_one_copy_of_every_category():
    """The categories are included from public/, never duplicated — two copies would drift."""
    from pathlib import Path

    editor = Path("templates/leads/_wedding_details_editor.html").read_text()
    for step in ("date", "venue", "who", "hotels", "times"):
        assert f'include "public/_wedding_step_{step}.html"' in editor


def test_the_editors_pickers_survive_escape_inside_the_modal():
    """flatpickr's own Escape handler closes its panel before any window listener sees the
    key, so without the fpJustClosed() guard an agent nudging the ceremony time and
    pressing Escape would lose every edit in the modal."""
    from pathlib import Path

    editor = Path("templates/leads/_wedding_details_editor.html").read_text()
    times = Path("templates/public/_wedding_step_times.html").read_text()
    assert "fpJustClosed()" in editor
    assert "data-flatpickr-time" in times
    # The attribute on a real element — the rule itself is quoted in a comment above it.
    assert '<input type="time"' not in times


def test_the_editor_uses_no_native_dialog(client, agent):
    lead = LeadFactory()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    body = client.get(f"{reverse('lead_detail', args=[lead.pk])}?wedding=1").content.decode()
    assert "weddingPlanner(" in body
    assert "window.confirm" not in body
    assert "window.alert" not in body
    assert "<dialog" not in body
