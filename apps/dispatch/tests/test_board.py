from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.dispatch import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

DAY = date(2026, 8, 26)


def _trip(**kwargs):
    """A booked trip on DAY unless overridden."""
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kwargs.setdefault("pickup_date", DAY)
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(lead=lead, **kwargs)


def test_board_shows_only_booked_trips_for_the_requested_day(logged_in_client):
    today = _trip()
    _trip(pickup_date=DAY + timedelta(days=1))
    _trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.status_code == 200
    assert [t.pk for t in resp.context["trips"]] == [today.pk]


def test_trips_are_ordered_by_pickup_time(logged_in_client):
    late = _trip(pickup_time=time(16, 30))
    early = _trip(pickup_time=time(6, 15))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert [t.pk for t in resp.context["trips"]] == [early.pk, late.pk]


def test_coverage_reflects_the_active_assignment(logged_in_client):
    uncovered = _trip(pickup_time=time(6, 0))
    offered = _trip(pickup_time=time(9, 0))
    confirmed = _trip(pickup_time=time(14, 0))
    services.send_offer(offered, VendorFactory(), payout=Decimal("120.00"))
    services.assign_direct(confirmed, VendorFactory(name="Capital"), payout=Decimal("200.00"))

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    coverage = {t.pk: t.coverage for t in resp.context["trips"]}
    assert coverage == {
        uncovered.pk: "uncovered",
        offered.pk: "offered",
        confirmed.pk: "confirmed",
    }
    assert resp.context["counts"] == {"uncovered": 1, "offered": 1, "confirmed": 1}
    assert b"Capital" in resp.content


def test_a_declined_offer_leaves_the_trip_uncovered(logged_in_client):
    trip = _trip()
    services.decline(services.send_offer(trip, VendorFactory(), payout=Decimal("120.00")))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.context["trips"][0].coverage == "uncovered"
    assert resp.context["counts"]["uncovered"] == 1


def test_cancelled_trips_stay_off_the_board(logged_in_client):
    live = _trip(pickup_time=time(6, 0))
    _trip(pickup_time=time(9, 0), trip_status=Reservation.TripStatus.CANCELLED)
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert [t.pk for t in resp.context["trips"]] == [live.pk]
    assert resp.context["counts"]["uncovered"] == 1


def test_route_ends_come_from_the_prefetch(logged_in_client):
    _trip(stops=["IAD", "The Jefferson"])
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    rendered = resp.context["trips"][0]
    assert rendered.pickup_stop.address == "IAD"
    assert rendered.dropoff_stop.address == "The Jefferson"
    assert b"IAD" in resp.content


def test_chip_filter_narrows_the_grid(logged_in_client):
    uncovered = _trip(pickup_time=time(6, 0))
    covered = _trip(pickup_time=time(9, 0))
    services.assign_direct(covered, VendorFactory(), payout=Decimal("120.00"))
    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "f": "uncovered"}
    )
    assert [t.pk for t in resp.context["trips"]] == [uncovered.pk]
    # counts stay whole-day so the strip doesn't collapse to the filter
    assert resp.context["counts"]["confirmed"] == 1


def test_board_defaults_to_today_and_pages_by_day(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_board"))
    assert resp.context["day"] == timezone.localdate()
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.context["prev_day"] == DAY - timedelta(days=1)
    assert resp.context["next_day"] == DAY + timedelta(days=1)


def test_a_bad_day_param_falls_back_to_today(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": "not-a-date"})
    assert resp.status_code == 200
    assert resp.context["day"] == timezone.localdate()


def test_board_requires_login(client):
    resp = client.get(reverse("dispatch_board"))
    assert resp.status_code == 302
    assert "/portal/login/" in resp["Location"]


def test_today_link_preserves_the_active_filter(logged_in_client):
    """Clicking Today while a strip filter is active should not silently clear it."""
    _trip(pickup_time=time(6, 0))
    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "f": "uncovered"}
    )
    today_str = timezone.localdate().isoformat()
    assert f'href="?day={today_str}&f=uncovered"'.encode() in resp.content


def test_board_query_count_does_not_grow_with_trips(
    logged_in_client, django_assert_max_num_queries
):
    for hour in range(8, 20):  # 12 trips, each with stops and a vendor
        trip = _trip(pickup_time=time(hour, 0))
        services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
    with django_assert_max_num_queries(15):
        logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
