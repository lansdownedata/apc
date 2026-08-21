from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.dispatch import selectors, services
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _trip(**kwargs):
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(lead=lead, **kwargs)


def test_vendors_are_ranked_by_how_often_they_are_used():
    trip = _trip()
    heavy, light, unused = VendorFactory(), VendorFactory(), VendorFactory()
    for _ in range(3):
        services.decline(services.send_offer(_trip(), heavy, payout=Decimal("10.00")))
    services.decline(services.send_offer(_trip(), light, payout=Decimal("10.00")))

    ranked = selectors.vendor_options(trip)
    assert [o["vendor"].pk for o in ranked[:2]] == [heavy.pk, light.pk]
    assert ranked[0]["used"] == 3
    assert unused.pk in [o["vendor"].pk for o in ranked]


def test_ranking_is_capped_at_the_limit():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    assert len(selectors.vendor_options(trip, limit=8)) == 8


def test_vendor_options_query_count_is_flat_regardless_of_vendor_count(
    django_assert_max_num_queries,
):
    """Guards against reintroducing an N+1, e.g. a per-vendor insurance lookup.

    The bound must hold at two very different vendor counts to prove the query count is
    flat, not just under some ceiling that a mild N+1 would still clear.
    """
    trip = _trip()
    for _ in range(3):
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)

    for _ in range(27):  # 30 active vendors total
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)


def test_search_looks_past_the_top_slice():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    needle = VendorFactory(name="Zebra Executive")
    assert [o["vendor"].pk for o in selectors.vendor_options(trip, search="zebra")] == [needle.pk]


def test_options_carry_vehicle_fit():
    suv = VehicleTypeFactory(name="SUV")
    trip = _trip(vehicle=suv)
    fitting = VendorFactory()
    fitting.vehicle_types.add(suv)
    VendorFactory()
    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[fitting.pk]["fits_vehicle"] is True
    assert all(o["fits_vehicle"] is False for pk, o in by_pk.items() if pk != fitting.pk)


def test_panel_renders_the_trip_and_vendors(logged_in_client):
    trip = _trip()
    VendorFactory(name="Capital Chauffeurs")
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 200
    assert b"Capital Chauffeurs" in resp.content
    assert resp.context["trip"].pk == trip.pk
    assert resp.context["assignment"] is None


def test_panel_shows_the_active_assignment_when_there_is_one(logged_in_client):
    trip = _trip()
    services.assign_direct(trip, VendorFactory(name="Chesapeake"), payout=Decimal("200.00"))
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.context["assignment"].vendor.name == "Chesapeake"
    assert b"Chesapeake" in resp.content


def test_panel_requires_login(client):
    trip = _trip()
    resp = client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 302
