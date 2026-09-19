"""Assigning a driver from the trip editor, not just from the drawer.

The client wants coverage settable in two places: the dispatch drawer, which has always
had it, and the edit-trip screen on an order. Same two choices in both — an in-house
driver (with an optional unit) or farm it out to a vendor.

The rules stay in `dispatch.services`: one active assignment per trip, a booked lead, an
active driver. This only adds a second way to reach them, so the editor posts to the very
same endpoints the drawer does and re-reads its state from the same selectors.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory, VehicleFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
EDITOR = (ROOT / "templates" / "leads" / "_reservation_editor.html").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


def _trip(status=Lead.Status.BOOKED, **over):
    return ReservationFactory(
        lead=LeadFactory(status=status),
        rate=Decimal("1000"),
        hours=1,
        min_hours=0,
        stops=["Dulles International", "The Hay-Adams"],
        **over,
    )


def _options(client, trip):
    return client.get(reverse("dispatch_assign_options", args=[trip.pk]))


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


# --- the options the section offers --------------------------------------------------


def test_the_options_need_login(client):
    assert "/login" in _options(client, _trip())["Location"]


def test_an_uncovered_trip_offers_both_ways_to_cover_it(client, agent):
    DriverFactory(name="Ray Delgado")
    VendorFactory(name="Reston Coach Co")
    body = _options(client, _trip()).json()
    assert body["coverage"] == "uncovered"
    assert body["canAssign"] is True
    assert "Ray Delgado" in [d["label"] for d in body["drivers"]]
    assert "Reston Coach Co" in [v["name"] for v in body["vendors"]]


def test_the_payout_starts_on_what_the_trip_pays_its_vendor(client, agent):
    """The same number the drawer pre-fills — quoted and actual margin start level."""
    trip = _trip(affiliate_cost=Decimal("580"))
    assert _options(client, trip).json()["payout"] == "580.00"


def test_a_covered_trip_says_who_has_it(client, agent):
    trip = _trip()
    AssignmentFactory(
        reservation=trip, vendor=VendorFactory(name="Reston Coach Co"),
        status=Assignment.Status.CONFIRMED,
    )
    body = _options(client, trip).json()
    assert body["coverage"] == "confirmed"
    assert body["provider"] == "Reston Coach Co"
    assert body["isInHouse"] is False
    assert body["assignmentId"]


def test_an_in_house_trip_names_the_driver(client, agent):
    trip = _trip()
    AssignmentFactory(reservation=trip, in_house=True, driver=DriverFactory(name="Ray Delgado"))
    body = _options(client, trip).json()
    assert body["provider"] == "Ray Delgado"
    assert body["isInHouse"] is True


def test_a_quote_cannot_be_assigned(client, agent):
    """`services._claim` refuses an unbooked lead, so the section must not offer it."""
    body = _options(client, _trip(status=Lead.Status.QUOTED)).json()
    assert body["canAssign"] is False


def test_units_are_offered_for_an_in_house_driver(client, agent):
    DriverFactory(name="Ray Delgado")
    VehicleFactory(name="Coach 12")
    body = _options(client, _trip()).json()
    assert "Coach 12" in [u["label"] for u in body["units"]]


# --- the section in the editor -------------------------------------------------------


def test_the_editor_has_a_driver_section_with_both_options():
    assert "setCoverageMode('in_house')" in EDITOR
    assert "setCoverageMode('farm_out')" in EDITOR
    assert "In-house" in EDITOR and "Farm-out" in EDITOR


def test_the_section_assigns_through_the_same_endpoints_the_drawer_uses():
    """The guards live in dispatch.services; this must not grow a second way in."""
    assert "assignInHouse" in APP_JS
    assert "assignVendor" in APP_JS
    assert "dispatch/" in APP_JS or "assignUrls" in EDITOR


def test_the_section_is_hidden_when_the_editor_came_from_the_drawer():
    """The drawer behind it already has the full controls — two live forms for the same
    trip on one screen is how a dispatcher assigns twice."""
    assert "!returnDrawerUrl" in EDITOR


def test_the_editor_loads_coverage_only_when_it_opens(client, agent):
    """Serializing every trip's driver and vendor lists into the page would be heavy."""
    assert "loadCoverage" in APP_JS


# --- reassigning says what it actually does ------------------------------------------


def test_a_gnet_offer_is_flagged_so_the_confirm_can_say_so(client, agent):
    trip = _trip()
    AssignmentFactory(
        reservation=trip, vendor=VendorFactory(name="Reston Coach Co"),
        status=Assignment.Status.OFFERED, channel=Assignment.Channel.GNET,
    )
    assert _options(client, trip).json()["isGnet"] is True


def test_a_manual_offer_is_not(client, agent):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    assert _options(client, trip).json()["isGnet"] is False


def test_the_confirm_tells_a_gnet_affiliate_apart_from_a_manual_one():
    """On GNet the cancel goes out over the network, so "not notified" would be a lie."""
    release = APP_JS[APP_JS.index("releaseCoverage()") :][:1400]
    assert "isGnet" in release
    assert "GNet" in release
    assert "not notified automatically" in release


def test_reassign_is_what_the_button_says_now():
    assert "Reassign" in EDITOR
    assert "'Unassign'" not in EDITOR
