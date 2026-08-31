"""Payload builders: our domain rows -> documented LA request shapes."""

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.integrations import la_sync
from apps.leads.factories import LeadFactory
from apps.reservations.factories import ReservationFactory, StopFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _geocoded_reservation(trip_type=Reservation.TripType.TRANSFER, **kwargs):
    res = ReservationFactory(
        lead=LeadFactory(),
        trip_type=trip_type,
        passengers=3,
        pickup_date=date(2026, 7, 15),
        pickup_time=time(10, 0),
        stops=[],
        **kwargs,
    )
    StopFactory(
        reservation=res,
        sequence=0,
        address="JFK Airport",
        latitude=Decimal("40.641300"),
        longitude=Decimal("-73.778100"),
    )
    StopFactory(
        reservation=res,
        sequence=1,
        address="Times Square",
        latitude=Decimal("40.758000"),
        longitude=Decimal("-73.985500"),
    )
    return res


def test_registration_payload():
    contact = ContactFactory(
        name="Jane Doe",
        email="jane@example.com",
        phone="+15551234567",
        company=CompanyFactory(name="Acme"),
    )
    payload = la_sync.build_registration_payload(contact, password="pw123")
    assert payload == {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "password": "pw123",
        "company": "Acme",
        "cellular_phone1": "+15551234567",
    }


def test_single_word_name_gets_placeholder_last_name():
    contact = ContactFactory(name="Cher", email="c@example.com")
    payload = la_sync.build_registration_payload(contact, password="pw")
    assert payload["first_name"] == "Cher"
    assert payload["last_name"]  # non-empty placeholder


def test_transfer_rate_lookup_payload_has_pickup_and_dropoff():
    res = _geocoded_reservation()
    payload = la_sync.build_rate_lookup_payload(res)
    assert payload["passenger_count"] == 3
    assert payload["pickup"]["address"]["latitude"] == pytest.approx(40.6413)
    assert payload["dropoff"]["address"]["address_line1"] == "Times Square"
    assert "scheduled_duration_in_minutes" not in payload
    assert payload["scheduled_pickup_at"]  # ISO datetime string


def test_hourly_rate_lookup_payload_has_duration_no_dropoff():
    res = _geocoded_reservation(
        trip_type=Reservation.TripType.HOURLY, hours=Decimal("3"), min_hours=Decimal("4")
    )
    payload = la_sync.build_rate_lookup_payload(res)
    assert payload["scheduled_duration_in_minutes"] == 180  # billed_hours = override 3
    assert "dropoff" not in payload


def test_transfer_without_two_stops_raises():
    res = ReservationFactory(lead=LeadFactory())
    StopFactory(
        reservation=res,
        sequence=0,
        address="Only stop",
        latitude=Decimal("1"),
        longitude=Decimal("1"),
    )
    with pytest.raises(la_sync.LASyncError):
        la_sync.build_rate_lookup_payload(res)


def test_ungeocode_stop_triggers_geocoding():
    res = ReservationFactory(
        lead=LeadFactory(),
        pickup_date=date(2026, 7, 15),
        pickup_time=time(10, 0),
    )
    StopFactory(reservation=res, sequence=0, address="A st")
    StopFactory(reservation=res, sequence=1, address="B st")
    with patch.object(la_sync.geocoding, "geocode_stop") as geo:
        geo.return_value = (Decimal("1.0"), Decimal("2.0"))
        la_sync.build_rate_lookup_payload(res)
    assert geo.call_count == 2


def test_booking_payload_carries_passenger_payment_and_notes():
    res = _geocoded_reservation(rate=Decimal("450"))
    res.lead.contact.name = "Jane Doe"
    res.lead.contact.save()
    payload = la_sync.build_booking_payload(res, 987654)
    assert payload["search_result_id"] == 987654
    assert payload["passengers"][0]["first_name"] == "Jane"
    assert "payment_type_id" in payload
    assert f"APC quote #{res.lead_id}" in payload["notes"]
    assert str(res.line_total) in payload["notes"]


def _with_flight(reservation, sequence, number):
    from apps.addresses.factories import AirportFactory
    from apps.addresses.models import Airline

    stop = reservation.stops.get(sequence=sequence)
    stop.airport = AirportFactory(ident=f"KT{sequence}", iata=f"T{sequence}")
    stop.airline = Airline.objects.get(iata="UA")
    stop.flight_number = number
    stop.save()


def test_booking_payload_carries_pickup_and_dropoff_flight_info():
    res = _geocoded_reservation()
    _with_flight(res, 0, "123")
    _with_flight(res, 1, "456")
    payload = la_sync.build_booking_payload(res, search_result_id=1)
    assert payload["pickup_flight_info"] == {
        "airport_code": "T0",
        "airline_code": "UA",
        "flight_number": "123",
    }
    assert payload["dropoff_flight_info"] == {
        "airport_code": "T1",
        "airline_code": "UA",
        "flight_number": "456",
    }


def test_booking_payload_omits_flight_info_keys_without_a_flight():
    payload = la_sync.build_booking_payload(_geocoded_reservation(), search_result_id=1)
    assert "pickup_flight_info" not in payload
    assert "dropoff_flight_info" not in payload


def test_hourly_booking_never_sends_dropoff_flight_info():
    res = _geocoded_reservation(trip_type=Reservation.TripType.HOURLY, hours=Decimal("3"))
    _with_flight(res, 1, "456")
    payload = la_sync.build_booking_payload(res, search_result_id=1)
    assert "dropoff_flight_info" not in payload


def test_pickup_at_for_a_pacific_trip_carries_the_pacific_offset():
    """Reproduction: make_aware() used TIME_ZONE, so a 10 AM LA pickup went to LA
    as 10 AM Eastern — three hours wrong on the wire."""
    res = _geocoded_reservation()
    res.pickup_timezone = "America/Los_Angeles"
    res.save(update_fields=["pickup_timezone"])
    assert la_sync._pickup_at(res) == "2026-07-15T10:00:00-07:00"
