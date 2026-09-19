"""The order detail page: one order, its trips, who is driving them, and its money.

An order is a BOOKED lead — there is no Order model — so this page is keyed on the lead pk
like every other order route. It shares its trip lines with the quote workspace
(`components/trip_line.html`) and its money block with it too
(`payments/_money_actions.html`, `payments/_ledger.html`), because sales, ops and dispatch
reading three different renderings of the same trip is the drift this page exists to stop.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory, StopFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _order(**over) -> Lead:
    return LeadFactory(status=Lead.Status.BOOKED, **over)


def _trip(lead, **over):
    trip = ReservationFactory(lead=lead, rate=Decimal("500"), hours=1, min_hours=0, **over)
    StopFactory(reservation=trip, sequence=0, address="Dulles International")
    StopFactory(reservation=trip, sequence=1, address="The Hay-Adams")
    return trip


def _url(lead) -> str:
    return reverse("order_detail", args=[lead.pk])


@pytest.fixture
def owner(client):
    user = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(user)
    return user


# --- getting there -----------------------------------------------------------------


def test_the_page_requires_login(client):
    resp = client.get(_url(_order()))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_an_agent_without_payments_access_sees_the_order_but_not_the_money_actions(client):
    """The same split the orders console and the workspace already use: any signed-in
    agent may read an order; only payments access may move money on it."""
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    order = _order()
    _trip(order)
    resp = client.get(_url(order))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "The Hay-Adams" in body
    assert "adminCardPay(" not in body


def test_a_lead_that_is_not_booked_goes_to_its_quote_instead(client, owner):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    resp = client.get(_url(lead))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("lead_detail", args=[lead.pk])


def test_the_orders_list_links_to_it(client, owner):
    order = _order()
    assert _url(order) in client.get(reverse("orders_list")).content.decode()


# --- the trips ---------------------------------------------------------------------


def test_an_order_shows_every_trip_on_it(client, owner):
    order = _order()
    _trip(order)
    _trip(order)
    body = client.get(_url(order)).content.decode()
    assert body.count('data-line="') == 2
    assert "The Hay-Adams" in body


def test_the_order_page_and_the_workspace_share_one_trip_line():
    """Two copies of this markup would drift the first time either screen changed."""
    from pathlib import Path

    order_page = Path("templates/orders/order_detail.html").read_text()
    workspace = Path("templates/leads/lead_detail.html").read_text()
    assert 'include "components/trip_line.html"' in order_page
    assert 'include "components/trip_line.html"' in workspace


def test_a_linked_set_reads_as_one_line_with_its_count(client, owner):
    """The same fold the quote uses (APC-14) — four coaches are one movement."""
    import uuid

    order = _order()
    key = uuid.uuid4()
    for _ in range(4):
        _trip(order, group_key=key)
    body = client.get(_url(order)).content.decode()
    assert body.count('data-line="') == 1
    assert "×4" in body


def test_an_order_with_no_trips_says_so(client, owner):
    assert "No trips on this order" in client.get(_url(_order())).content.decode()


# --- the driver section ------------------------------------------------------------


def test_every_trip_offers_a_driver_section_that_opens_the_assign_drawer(client, owner):
    order = _order()
    trip = _trip(order)
    body = client.get(_url(order)).content.decode()
    assert reverse("dispatch_assign_panel", args=[trip.pk]) in body
    assert "drawer-open" in body
    assert "data-drawer" in body  # the drawer itself is on the page


def test_an_unassigned_trip_reads_unassigned(client, owner):
    order = _order()
    _trip(order)
    assert "Unassigned" in client.get(_url(order)).content.decode()


def test_a_farmed_out_trip_names_its_vendor(client, owner):
    order = _order()
    trip = _trip(order)
    AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Reston Coach Co"),
        status=Assignment.Status.CONFIRMED,
    )
    body = client.get(_url(order)).content.decode()
    assert "Reston Coach Co" in body
    assert "Unassigned" not in body


def test_an_offered_trip_is_not_yet_covered(client, owner):
    order = _order()
    trip = _trip(order)
    AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    assert "Offered" in client.get(_url(order)).content.decode()


def test_an_in_house_trip_names_its_driver(client, owner):
    order = _order()
    trip = _trip(order)
    AssignmentFactory(reservation=trip, in_house=True, driver=DriverFactory(name="Ray Delgado"))
    assert "Ray Delgado" in client.get(_url(order)).content.decode()


def test_a_declined_offer_puts_the_trip_back_to_unassigned(client, owner):
    """Coverage is derived from the active assignment, never stored — no cleanup needed."""
    order = _order()
    trip = _trip(order)
    AssignmentFactory(reservation=trip, status=Assignment.Status.DECLINED)
    assert "Unassigned" in client.get(_url(order)).content.decode()


# --- the money ---------------------------------------------------------------------


def test_the_money_block_is_the_one_the_workspace_already_uses(client, owner, settings):
    settings.STRIPE_PUBLISHABLE_KEY = "pk_test_123"
    order = _order()
    _trip(order)
    body = client.get(_url(order)).content.decode()
    assert "adminCardPay(" in body
    assert "sendPayLink(" in body
    assert "Journal" in body or "ledger" in body.lower()


# --- cost ---------------------------------------------------------------------------


def test_the_page_costs_the_same_whether_it_holds_one_trip_or_ten(client, owner):
    """`Reservation.pickup`/`dropoff` are properties over `stops.order_by(...)` that bypass
    any prefetch — two extra queries per row. The line reads the prefetched stops instead,
    and coverage arrives on one prefetch rather than a lookup per trip."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    small = _order()
    _trip(small)
    client.get(_url(small))  # warm whatever the first request caches

    with CaptureQueriesContext(connection) as one_trip:
        client.get(_url(small))

    big = _order()
    for _ in range(10):
        trip = _trip(big)
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    with CaptureQueriesContext(connection) as ten_trips:
        client.get(_url(big))

    assert len(ten_trips) <= len(one_trip)
