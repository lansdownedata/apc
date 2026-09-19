"""The trip editor is its own component, so every screen that lists trips can open it.

It used to live inside `quoteWorkspace()` and read ~45 of that component's members, which
meant the only page that could edit a trip was the quote. The order page had to link back
to the quote to change a pickup time, and the dispatch drawer could not offer it at all.

`reservationEditor()` now owns the modal and listens on the window, so a trip line opens it
with `$dispatch("reservation-edit", {id})` from whatever scope it happens to sit in.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory, StopFactory

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
EDITOR = (ROOT / "templates" / "leads" / "_reservation_editor.html").read_text()
TRIP_LINE = (ROOT / "templates" / "components" / "trip_line.html").read_text()


def _order() -> Lead:
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=lead, rate=Decimal("500"), hours=1, min_hours=0)
    StopFactory(reservation=trip, sequence=0, address="Dulles International")
    StopFactory(reservation=trip, sequence=1, address="The Hay-Adams")
    return lead


@pytest.fixture
def agent(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


# --- the component stands on its own ------------------------------------------------


def test_the_editor_is_its_own_alpine_component():
    assert "function reservationEditor(" in APP_JS
    assert "window.reservationEditor = reservationEditor" in APP_JS


def test_the_editor_partial_carries_its_own_scope():
    """It used to have no x-data at all and borrow the workspace's."""
    assert "reservationEditor(" in EDITOR


def test_the_editor_opens_from_anywhere_on_the_page():
    """A window event, the same way the dispatch drawer is opened."""
    assert "reservation-edit" in APP_JS
    assert "reservation-new" in APP_JS
    assert "$dispatch('reservation-edit'" in TRIP_LINE


def test_the_workspace_no_longer_owns_the_editor():
    workspace = APP_JS[
        APP_JS.index("function quoteWorkspace(") : APP_JS.index("function reservationEditor(")
    ]
    for moved in ("blankReservation(", "saveReservation(", "applyCostPrice(", "verifyPill("):
        assert moved not in workspace, moved


# --- both screens can edit a trip ---------------------------------------------------


def _body(client, url) -> str:
    return client.get(url).content.decode()


def test_the_order_page_can_edit_a_trip_in_place(client, agent):
    lead = _order()
    body = _body(client, reverse("order_detail", args=[lead.pk]))
    assert "reservationEditor(" in body
    assert "$dispatch('reservation-edit'" in body
    # and no longer bounces back to the quote to do it
    assert f"{reverse('lead_detail', args=[lead.pk])}?edit=" not in body


def test_the_quote_workspace_still_edits_a_trip(client, agent):
    lead = _order()
    body = _body(client, reverse("lead_detail", args=[lead.pk]))
    assert "reservationEditor(" in body
    assert "$dispatch('reservation-edit'" in body


def test_the_order_page_can_add_a_trip(client, agent):
    body = _body(client, reverse("order_detail", args=[_order().pk]))
    assert "$dispatch('reservation-new')" in body


def test_each_vehicle_in_a_set_is_editable_from_the_order_page(client, agent):
    """A linked set expands to its coaches; each one is its own trip."""
    import uuid

    lead = LeadFactory(status=Lead.Status.BOOKED)
    key = uuid.uuid4()
    members = []
    for _ in range(3):
        trip = ReservationFactory(
            lead=lead, rate=Decimal("500"), hours=1, min_hours=0, group_key=key
        )
        StopFactory(reservation=trip, sequence=0, address="Dulles International")
        StopFactory(reservation=trip, sequence=1, address="The Hay-Adams")
        members.append(trip)
    body = _body(client, reverse("order_detail", args=[lead.pk]))
    for trip in members:
        assert f"$dispatch('reservation-edit', {{ id: {trip.pk} }})" in body, trip.pk


def test_both_screens_offer_one_copy_of_the_editor():
    """Two copies on one page would collide on the Tom Select ids inside it."""
    order_page = (ROOT / "templates" / "orders" / "order_detail.html").read_text()
    workspace = (ROOT / "templates" / "leads" / "lead_detail.html").read_text()
    for page in (order_page, workspace):
        assert page.count('include "leads/_reservation_editor.html"') == 1


# --- saving stays on the page you saved from ----------------------------------------


def test_saving_returns_to_the_page_it_was_opened_from(client, agent):
    """It used to navigate to the quote, which threw an ops user off their order."""
    assert "window.location = r.url" not in APP_JS
    assert "location.reload()" in APP_JS


def test_a_trip_saved_from_the_order_page_is_still_saved(client, agent):
    lead = _order()
    trip = lead.reservations.first()
    resp = client.post(
        reverse("reservation_save"),
        data={
            "lead_id": lead.pk,
            "id": trip.pk,
            "tripType": "transfer",
            "pax": 9,
            "rate": "725",
            "stops": [{"address": "Dulles International"}, {"address": "The Watergate"}],
        },
        content_type="application/json",
    )
    assert resp.status_code in (200, 302)
    trip.refresh_from_db()
    assert trip.passengers == 9
    assert trip.rate == Decimal("725")
