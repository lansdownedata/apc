"""APC-16 — Reverse Route: flip a saved itinerary end-for-end, in place."""

from datetime import time

import pytest
from django.urls import reverse as urlreverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.notifications.models import Notification
from apps.reservations import groups
from apps.reservations.factories import FlightFactory, TransferReservationFactory
from apps.reservations.models import FlightDirection, Stop
from apps.reservations.routing import reverse_route

pytestmark = pytest.mark.django_db


def _route(reservation) -> list[str]:
    return [s.address for s in reservation.stops.order_by("sequence")]


def _three_stop(reservation, *, mid_time=time(15, 30)):
    reservation.stops.all().delete()
    Stop.objects.create(reservation=reservation, sequence=0, address="Venue")
    Stop.objects.create(
        reservation=reservation, sequence=1, address="Hotel", scheduled_time=mid_time
    )
    Stop.objects.create(reservation=reservation, sequence=2, address="Airport")
    return reservation


def test_reverse_route_flips_pickup_and_dropoff():
    res = _three_stop(TransferReservationFactory())

    reverse_route(res)

    assert _route(res) == ["Airport", "Hotel", "Venue"]


def test_reverse_route_clears_intermediate_stop_times():
    res = _three_stop(TransferReservationFactory(), mid_time=time(15, 30))

    reverse_route(res)

    mid = res.stops.order_by("sequence")[1]
    assert mid.scheduled_time is None


def test_reverse_route_gives_the_endpoints_the_trips_own_times():
    res = _three_stop(TransferReservationFactory())
    res.pickup_time = time(9, 0)
    res.dropoff_time = time(11, 30)
    res.save(update_fields=["pickup_time", "dropoff_time"])

    reverse_route(res)

    stops = list(res.stops.order_by("sequence"))
    assert stops[0].scheduled_time == time(9, 0)
    assert stops[-1].scheduled_time == time(11, 30)


def test_reverse_route_rederives_flight_direction_on_the_new_endpoints():
    iad = AirportFactory(iata="IAD", timezone="America/New_York")
    ua = AirlineFactory(iata="UA")
    res = TransferReservationFactory()
    res.stops.all().delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="IAD",
        airport=iad,
        airline=ua,
        flight_number="123",
        flight_direction=FlightDirection.ARRIVAL,
    )
    Stop.objects.create(reservation=res, sequence=1, address="Venue")

    reverse_route(res)

    airport_stop = res.stops.get(airport=iad)
    assert airport_stop.sequence == 1
    assert airport_stop.flight_direction == FlightDirection.DEPARTURE


def test_reverse_route_drops_a_flight_cache_row_whose_direction_flipped():
    iad = AirportFactory(iata="IAD", timezone="America/New_York")
    ua = AirlineFactory(iata="UA")
    flight = FlightFactory(airline=ua, airport=iad, direction=FlightDirection.ARRIVAL)
    res = TransferReservationFactory()
    res.stops.all().delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="IAD",
        airport=iad,
        airline=ua,
        flight_number=flight.flight_number,
        flight_direction=FlightDirection.ARRIVAL,
        flight=flight,
    )
    Stop.objects.create(reservation=res, sequence=1, address="Venue")

    reverse_route(res)

    assert res.stops.get(airport=iad).flight_id is None


def test_reverse_route_keeps_a_middle_airport_stops_chosen_direction():
    iad = AirportFactory(iata="IAD", timezone="America/New_York")
    ua = AirlineFactory(iata="UA")
    res = TransferReservationFactory()
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, address="Venue")
    Stop.objects.create(
        reservation=res,
        sequence=1,
        address="IAD",
        airport=iad,
        airline=ua,
        flight_number="99",
        flight_direction=FlightDirection.DEPARTURE,
    )
    Stop.objects.create(reservation=res, sequence=2, address="Hotel")

    reverse_route(res)

    mid = res.stops.get(airport=iad)
    assert mid.sequence == 1
    assert mid.flight_direction == FlightDirection.DEPARTURE


def test_reverse_route_refreshes_the_pickup_timezone():
    la = AirportFactory(iata="LAX", timezone="America/Los_Angeles")
    res = TransferReservationFactory(pickup_timezone="America/New_York")
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, address="NYC")
    Stop.objects.create(reservation=res, sequence=1, address="LAX", airport=la)

    reverse_route(res)

    res.refresh_from_db()
    assert res.pickup_timezone == "America/Los_Angeles"


def test_reverse_route_fans_out_to_a_linked_set():
    res = _three_stop(TransferReservationFactory())
    groups.set_group_size(res, 3)
    res.refresh_from_db()

    reverse_route(res, propagate=True)

    for member in res.lead.reservations.all():
        assert _route(member) == ["Airport", "Hotel", "Venue"]


# --- the view ------------------------------------------------------------------------


def _reverse(client, reservation):
    return client.post(urlreverse("reservation_reverse", args=[reservation.pk]))


def test_reverse_view_flips_the_route_and_redirects_to_the_lead(client):
    res = _three_stop(TransferReservationFactory())
    client.force_login(UserFactory())

    resp = _reverse(client, res)

    assert resp.status_code == 302
    assert resp.url == urlreverse("lead_detail", args=[res.lead_id])
    assert _route(res) == ["Airport", "Hotel", "Venue"]


def test_reverse_view_requires_login(client):
    res = _three_stop(TransferReservationFactory())

    resp = _reverse(client, res)

    assert resp.status_code == 302
    assert "/login" in resp.url
    assert _route(res) == ["Venue", "Hotel", "Airport"]


def test_reverse_view_flags_an_la_synced_trip_as_stale(client):
    res = _three_stop(TransferReservationFactory(la_reservation_id="LA-9"))
    client.force_login(UserFactory())

    _reverse(client, res)

    assert Notification.objects.filter(lead=res.lead, kind=Notification.Kind.LA_CHANGED).exists()


def test_reverse_view_reverses_every_trip_in_a_linked_set(client):
    res = _three_stop(TransferReservationFactory())
    groups.set_group_size(res, 2)
    client.force_login(UserFactory())

    _reverse(client, res)

    for member in res.lead.reservations.all():
        assert _route(member) == ["Airport", "Hotel", "Venue"]
