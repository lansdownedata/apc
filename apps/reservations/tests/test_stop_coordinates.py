from decimal import Decimal
from unittest import mock

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.views import _reservation_draft
from apps.reservations.drafts import parse_draft, save_reservation_from_draft
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _payload(stops):
    return {
        "tripType": Reservation.TripType.TRANSFER,
        "service": "Airport transfer",
        "date": "2026-08-01",
        "time": "09:00",
        "dropoffDate": "2026-08-01",
        "dropoffTime": "11:00",
        "pax": 2,
        "rate": "150",
        "stops": stops,
    }


AIRPORT_STOP = {
    "address": "Boston Logan International Airport, Boston, MA",
    "lat": "42.361970",
    "lng": "-71.007900",
}
PLAIN_STOP = {"address": "14 Beacon Street, Boston, MA"}


def test_parse_draft_keeps_stop_coordinates():
    data = parse_draft(_payload([AIRPORT_STOP, PLAIN_STOP]))
    assert data["stops"][0]["latitude"] == Decimal("42.361970")
    assert data["stops"][0]["longitude"] == Decimal("-71.007900")


def test_parse_draft_tolerates_missing_coordinates():
    data = parse_draft(_payload([AIRPORT_STOP, PLAIN_STOP]))
    assert data["stops"][1]["latitude"] is None
    assert data["stops"][1]["longitude"] is None


def test_parse_draft_rejects_out_of_range_coordinates():
    bad = {"address": "somewhere", "lat": "999", "lng": "0"}
    data = parse_draft(_payload([bad, PLAIN_STOP]))
    assert data["stops"][0]["latitude"] is None


def test_parse_draft_ignores_unparseable_coordinates():
    bad = {"address": "somewhere", "lat": "not-a-number", "lng": ""}
    data = parse_draft(_payload([bad, PLAIN_STOP]))
    assert data["stops"][0]["latitude"] is None


def test_save_persists_stop_coordinates():
    lead = LeadFactory()
    reservation = save_reservation_from_draft(lead, _payload([AIRPORT_STOP, PLAIN_STOP]))
    pickup = reservation.ordered_stops.first()
    assert pickup.latitude == Decimal("42.361970")
    assert pickup.longitude == Decimal("-71.007900")


def test_serializer_emits_stop_coordinates():
    lead = LeadFactory()
    reservation = save_reservation_from_draft(lead, _payload([AIRPORT_STOP, PLAIN_STOP]))
    draft = _reservation_draft(reservation)
    assert draft["stops"][0]["lat"] == "42.361970"
    assert draft["stops"][0]["lng"] == "-71.007900"
    assert draft["stops"][1]["lat"] == ""


def test_coordinates_survive_a_save_reload_save_round_trip():
    """Stops are deleted and recreated on every save — coordinates must round-trip
    through the draft payload or the second save silently drops them."""
    lead = LeadFactory()
    reservation = save_reservation_from_draft(lead, _payload([AIRPORT_STOP, PLAIN_STOP]))

    reloaded = _reservation_draft(Reservation.objects.get(pk=reservation.pk))
    reloaded["service"] = "Edited after reload"
    save_reservation_from_draft(lead, reloaded, instance=reservation)

    pickup = Reservation.objects.get(pk=reservation.pk).ordered_stops.first()
    assert pickup.latitude == Decimal("42.361970")
    assert pickup.longitude == Decimal("-71.007900")


def test_geocode_stop_makes_no_request_when_coordinates_are_cached():
    from apps.integrations.geocoding import geocode_stop

    lead = LeadFactory()
    reservation = save_reservation_from_draft(lead, _payload([AIRPORT_STOP, PLAIN_STOP]))
    pickup = reservation.ordered_stops.first()
    with mock.patch("apps.integrations.geocoding.requests.get") as get:
        lat, lng = geocode_stop(pickup)
    get.assert_not_called()
    assert lat == Decimal("42.361970")
