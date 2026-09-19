"""Who is driving a trip, shown on the quote workspace's trip lines.

The same `components/trip_line.html` the order page renders, so an agent looking at a
booked order in sales sees the coverage a dispatcher sees. Assignment needs a BOOKED lead
(`dispatch.services._claim`), so on a quote the section is absent rather than inert.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory, StopFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _trip(lead):
    trip = ReservationFactory(lead=lead, rate=Decimal("500"), hours=1, min_hours=0)
    StopFactory(reservation=trip, sequence=0, address="Dulles International")
    StopFactory(reservation=trip, sequence=1, address="The Hay-Adams")
    return trip


def _workspace(client, lead) -> str:
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


def test_a_booked_trip_shows_who_is_driving_it(client, agent):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = _trip(lead)
    AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Reston Coach Co"),
        status=Assignment.Status.CONFIRMED,
    )
    body = _workspace(client, lead)
    assert "Reston Coach Co" in body
    assert reverse("dispatch_assign_panel", args=[trip.pk]) in body


def test_an_uncovered_booked_trip_asks_to_be_assigned(client, agent):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = _trip(lead)
    body = _workspace(client, lead)
    assert reverse("dispatch_assign_panel", args=[trip.pk]) in body
    assert "Unassigned" in body


def test_an_in_house_trip_names_its_driver(client, agent):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = _trip(lead)
    AssignmentFactory(reservation=trip, in_house=True, driver=DriverFactory(name="Ray Delgado"))
    assert "Ray Delgado" in _workspace(client, lead)


def test_a_quote_has_no_driver_section_to_offer(client, agent):
    """Nothing can be assigned until the lead is booked, so nothing is shown."""
    lead = LeadFactory(status=Lead.Status.QUOTED)
    trip = _trip(lead)
    body = _workspace(client, lead)
    assert reverse("dispatch_assign_panel", args=[trip.pk]) not in body
    assert "data-drawer" not in body


def test_the_sales_actions_stay_on_the_quote_workspace(client, agent):
    """Duplicate, copy-to-dates, reverse and delete are how a quote is built; an order page
    showing them would invite editing a booked trip's shape by accident."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    _trip(lead)
    assert "Copy to dates" in _workspace(client, lead)


def test_the_workspace_still_costs_a_flat_number_of_queries(client, agent):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    small = LeadFactory(status=Lead.Status.BOOKED)
    _trip(small)
    _workspace(client, small)

    with CaptureQueriesContext(connection) as one_trip:
        _workspace(client, small)

    big = LeadFactory(status=Lead.Status.BOOKED)
    for _ in range(8):
        AssignmentFactory(reservation=_trip(big), status=Assignment.Status.CONFIRMED)
    with CaptureQueriesContext(connection) as eight_trips:
        _workspace(client, big)

    assert len(eight_trips) <= len(one_trip)
