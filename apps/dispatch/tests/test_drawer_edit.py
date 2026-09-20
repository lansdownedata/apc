"""Editing a trip from the dispatch drawer.

The board shows trips from many leads at once, so it cannot serialize every trip's draft
into the page the way the quote and the order page do — a week's view would carry hundreds.
The editor fetches the one trip it is opening instead, and `lead_id` rides with it so the
save still posts against the right lead.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _trip(**over):
    lead = over.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    return ReservationFactory(
        lead=lead,
        rate=Decimal("650"),
        hours=1,
        min_hours=0,
        passengers=12,
        stops=["Dulles International", "The Hay-Adams"],
        **over,
    )


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


# --- the draft endpoint -------------------------------------------------------------


def test_the_draft_endpoint_requires_login(client):
    trip = _trip()
    resp = client.get(reverse("reservation_draft", args=[trip.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_one_trip_comes_back_as_a_draft_the_editor_can_open(client, agent):
    trip = _trip()
    body = client.get(reverse("reservation_draft", args=[trip.pk])).json()
    assert body["leadId"] == trip.lead_id
    assert body["draft"]["id"] == trip.pk
    assert body["draft"]["pax"] == 12
    assert [s["address"] for s in body["draft"]["stops"]] == [
        "Dulles International",
        "The Hay-Adams",
    ]


def test_the_draft_carries_the_size_of_its_linked_set(client, agent):
    """Saving with applyToGroup has to know whether this trip stands alone (APC-14)."""
    import uuid

    lead = LeadFactory(status=Lead.Status.BOOKED)
    key = uuid.uuid4()
    trips = [_trip(lead=lead, group_key=key) for _ in range(3)]
    body = client.get(reverse("reservation_draft", args=[trips[0].pk])).json()
    assert body["draft"]["quantity"] == 3


def test_an_unknown_trip_is_a_404(client, agent):
    assert client.get(reverse("reservation_draft", args=[999999])).status_code == 404


# --- the drawer ---------------------------------------------------------------------


def test_the_drawer_offers_edit_trip(client, agent):
    trip = _trip()
    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert "Edit trip" in body
    assert f"reservation-edit', {{ id: {trip.pk}," in body


def test_the_board_carries_the_editor_and_tells_it_where_to_fetch(client, agent):
    body = client.get(reverse("dispatch_board")).content.decode()
    assert "reservationEditor(" in body
    assert "draftUrl" in body
    # once per page — the Tom Select ids inside the editor are global
    assert body.count("reservationEditor(") == 1


def test_the_editor_fetches_a_trip_it_was_not_given(client, agent):
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    assert "draftUrl" in app_js
    # the lead the save posts against comes from the fetched trip, not the page
    assert "draftLeadId" in app_js


def test_the_board_still_opens_the_assign_drawer(client, agent):
    """The editor is a second modal on the page; the drawer must be untouched."""
    body = client.get(reverse("dispatch_board")).content.decode()
    assert "data-drawer" in body
    assert "drawer-open" in body


# --- the editor opens in front of the drawer, and hands it back ----------------------

ROOT = Path(__file__).resolve().parents[3]


def _z(template: str) -> int:
    """The stacking level a modal/drawer template puts itself at."""
    found = re.findall(r"z-\[?(\d+)\]?", (ROOT / "templates" / template).read_text())
    return max(int(n) for n in found)


def test_the_editor_opens_in_front_of_the_drawer():
    """It opened behind it — the drawer's own backdrop greyed the editor out."""
    assert _z("leads/_reservation_editor.html") > _z("components/drawer.html")


def test_the_confirm_modal_still_wins_over_the_editor():
    """Saving a smaller quantity asks to remove vehicles; that has to be reachable."""
    assert _z("components/modal.html") > _z("leads/_reservation_editor.html")
    assert _z("components/toasts.html") > _z("components/modal.html")


def test_the_drawer_tells_the_editor_where_it_came_from(client, agent):
    trip = _trip()
    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert reverse("dispatch_assign_panel", args=[trip.pk]) in body
    assert "drawerUrl" in body


def test_an_existing_trip_offers_save_and_save_and_close():
    editor = (ROOT / "templates" / "leads" / "_reservation_editor.html").read_text()
    assert "saveReservation(false)" in editor  # save, stay put
    assert "saveReservation(true)" in editor  # save, close back to where you came from
    assert "Save &amp; close" in editor or "Save & close" in editor


def test_saving_from_the_drawer_puts_the_drawer_back_instead_of_reloading():
    app_js = (ROOT / "static" / "js" / "app.js").read_text()
    close = app_js[app_js.index("closeEditor()") : app_js.index("closeEditor()") + 900]
    assert "returnDrawerUrl" in close
    assert "drawer-open" in close


# --- choosing how to cover it --------------------------------------------------------


def test_the_drawer_offers_a_way_to_choose_in_house_or_farm_out(client, agent):
    """The two forms used to sit stacked with nothing saying they were alternatives."""
    from apps.fleet.factories import DriverFactory
    from apps.vendors.factories import VendorFactory

    DriverFactory(name="Ray Delgado")
    VendorFactory(name="Reston Coach Co")
    body = client.get(reverse("dispatch_assign_panel", args=[_trip().pk])).content.decode()
    assert "mode = 'in_house'" in body
    assert "mode = 'farm_out'" in body
    assert "In-house" in body and "Farm-out" in body


def test_with_no_drivers_on_the_roster_it_opens_on_farm_out(client, agent):
    from apps.vendors.factories import VendorFactory

    VendorFactory(name="Reston Coach Co")
    body = client.get(reverse("dispatch_assign_panel", args=[_trip().pk])).content.decode()
    assert "mode: 'farm_out'" in body
    # no roster, no choice to offer — the chooser stays away entirely
    assert "mode = 'in_house'" not in body


def test_a_covered_trip_shows_no_chooser(client, agent):
    """Coverage is one active assignment; changing it means withdrawing first."""
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment

    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert "mode = 'farm_out'" not in body
    # Renamed 2026-09-19: the control withdraws and leaves the trip uncovered, ready to
    # pick someone else, so it says what it is for.
    assert "Reassign" in body
