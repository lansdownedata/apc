"""APC-15 — Create Return Trip: one click turns an outbound into its reversed return."""

from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.reservations import groups
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation, Stop
from apps.reservations.routing import create_return_trip

pytestmark = pytest.mark.django_db


def _outbound(**over):
    res = TransferReservationFactory(
        pickup_date=date(2026, 7, 4),
        pickup_time=time(14, 0),
        dropoff_date=date(2026, 7, 4),
        dropoff_time=time(15, 30),
        rate=Decimal("225"),
        hours=Decimal("3"),
        gratuity_pct=Decimal("20"),
        **over,
    )
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, address="Venue")
    Stop.objects.create(reservation=res, sequence=1, address="Hotel", scheduled_time=time(14, 40))
    Stop.objects.create(reservation=res, sequence=2, address="Airport")
    return res


def test_return_trip_is_the_route_reversed():
    out = _outbound()

    ret = create_return_trip(out)

    assert [s.address for s in ret.stops.order_by("sequence")] == ["Airport", "Hotel", "Venue"]


def test_return_trip_clears_the_schedule_for_the_dispatcher_to_fill():
    out = _outbound()

    ret = create_return_trip(out)

    assert ret.pickup_date is None
    assert ret.pickup_time is None
    assert ret.dropoff_date is None
    assert ret.dropoff_time is None


def test_return_trip_keeps_the_vehicle_and_pricing():
    out = _outbound()

    ret = create_return_trip(out)

    assert ret.vehicle_id == out.vehicle_id
    assert ret.rate == Decimal("225")
    assert ret.hours == Decimal("3")
    assert ret.gratuity_pct == Decimal("20")


def test_return_trip_is_a_new_unlinked_reservation():
    out = _outbound()

    ret = create_return_trip(out)

    assert ret.pk != out.pk
    assert ret.group_key is None
    assert ret.lead_id == out.lead_id


def test_return_trip_of_a_grouped_outbound_is_a_single_standalone_trip():
    out = _outbound()
    groups.set_group_size(out, 3)
    out.refresh_from_db()

    ret = create_return_trip(out)

    assert ret.group_key is None
    assert out.lead.reservations.filter(group_key=None).count() == 1


def test_return_trip_starts_with_no_la_link_or_recognised_revenue():
    out = _outbound(
        la_reservation_id="LA-5",
        trip_status=Reservation.TripStatus.DISPATCHED,
        revenue_status=Reservation.RevenueStatus.RECOGNIZED,
        recognized_amount=Decimal("400"),
    )

    ret = create_return_trip(out)

    assert ret.la_reservation_id == ""
    assert ret.trip_status == ""
    assert ret.revenue_status == Reservation.RevenueStatus.DEFERRED
    assert ret.recognized_amount == 0


def test_return_trip_is_appended_after_the_leads_last_trip():
    out = _outbound()
    TransferReservationFactory(lead=out.lead, sort_order=9)

    ret = create_return_trip(out)

    assert ret.sort_order == 10


def test_return_trip_resolves_its_own_pickup_timezone_from_the_new_origin():
    lax = AirportFactory(iata="LAX", timezone="America/Los_Angeles")
    ua = AirlineFactory(iata="UA")
    out = TransferReservationFactory(pickup_timezone="America/New_York")
    out.stops.all().delete()
    Stop.objects.create(reservation=out, sequence=0, address="NYC hotel")
    Stop.objects.create(
        reservation=out, sequence=1, address="LAX", airport=lax, airline=ua, flight_number="9"
    )

    ret = create_return_trip(out)

    assert ret.pickup_timezone == "America/Los_Angeles"


# --- the view ------------------------------------------------------------------------


def _return(client, reservation):
    return client.post(reverse("reservation_return", args=[reservation.pk]))


def test_return_view_creates_the_trip_and_redirects_into_the_editor(client):
    out = _outbound()
    client.force_login(UserFactory())

    resp = _return(client, out)

    ret = out.lead.reservations.exclude(pk=out.pk).get()
    assert resp.status_code == 302
    assert resp.url == f"{reverse('lead_detail', args=[out.lead_id])}?edit={ret.pk}"


def test_return_view_requires_login(client):
    out = _outbound()

    resp = _return(client, out)

    assert resp.status_code == 302
    assert "/login" in resp.url
    assert out.lead.reservations.count() == 1
