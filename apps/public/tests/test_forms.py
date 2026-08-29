import json
from decimal import Decimal

import pytest

from apps.addresses.models import Airline, Airport
from apps.public.forms import OCCASION_CHOICES, BookingRequestForm


def _base(**over):
    data = {"name": "Jane Rider", "email": "jane@example.com", "passengers": 2}
    data.update(over)
    return data


def test_occasion_choices_drop_hourly_charter():
    """Hourly is a trip_type now, not an occasion."""
    assert all(v != "Hourly Charter" for v, _ in OCCASION_CHOICES)
    assert all(v != "Special Event" for v, _ in OCCASION_CHOICES)


def test_trip_type_defaults_to_transfer_when_absent():
    form = BookingRequestForm(_base())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["trip_type"] == "transfer"


def test_hourly_requires_hours():
    form = BookingRequestForm(_base(trip_type="hourly"))
    assert not form.is_valid()
    assert "hours" in form.errors


def test_hourly_with_hours_is_valid():
    form = BookingRequestForm(_base(trip_type="hourly", hours="4"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["hours"] == Decimal("4")


def test_transfer_nulls_stale_hours():
    """Fill hours, toggle back to Transfer, submit — the duration must not persist."""
    form = BookingRequestForm(_base(trip_type="transfer", hours="4"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["hours"] is None


def test_occasion_is_optional():
    form = BookingRequestForm(_base(service=""))
    assert form.is_valid(), form.errors


def test_ordered_stops_pickup_mids_dropoff():
    form = BookingRequestForm(
        _base(
            pickup="123 Main St, Ashburn, VA",
            pickup_lat="38.9",
            pickup_lng="-77.4",
            dropoff="Dulles Intl (IAD)",
            dropoff_lat="38.95",
            dropoff_lng="-77.45",
            stops_json=json.dumps([{"address": "Reston Town Center", "lat": 38.95, "lng": -77.35}]),
        )
    )
    assert form.is_valid(), form.errors
    stops = form.cleaned_data["stops"]
    assert [s["address"] for s in stops] == [
        "123 Main St, Ashburn, VA",
        "Reston Town Center",
        "Dulles Intl (IAD)",
    ]
    assert stops[0]["lat"] == 38.9
    # suite/unit was removed from the booking form — stops carry no suite key
    assert "suite" not in stops[0]


def test_invalid_email_rejected():
    """The email field validates format server-side (EmailField)."""
    form = BookingRequestForm(_base(email="not-an-email"))
    assert not form.is_valid()
    assert "email" in form.errors


def test_valid_email_accepted():
    form = BookingRequestForm(_base(email="rider@example.com"))
    assert form.is_valid(), form.errors


def test_no_addresses_yields_empty_stops():
    form = BookingRequestForm(_base())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["stops"] == []


def test_malformed_stops_json_is_rejected_not_500():
    form = BookingRequestForm(_base(stops_json="{not json"))
    assert not form.is_valid()
    assert "stops_json" in form.errors


def test_too_many_stops_rejected():
    payload = [{"address": f"Stop {i}"} for i in range(5)]
    form = BookingRequestForm(_base(stops_json=json.dumps(payload)))
    assert not form.is_valid()
    assert "stops_json" in form.errors


def test_blank_address_stops_dropped():
    form = BookingRequestForm(
        _base(
            pickup="",
            dropoff="",
            stops_json=json.dumps([{"address": ""}, {"address": "Real Stop"}]),
        )
    )
    assert form.is_valid(), form.errors
    assert [s["address"] for s in form.cleaned_data["stops"]] == ["Real Stop"]


@pytest.fixture
def iad(db):
    return Airport.objects.get(iata="IAD")  # seeded by addresses.0003


@pytest.fixture
def united(db):
    return Airline.objects.get(iata="UA")


def test_pickup_flight_fields_land_on_the_pickup_stop(iad, united):
    form = BookingRequestForm(
        _base(
            pickup="Dulles",
            pickup_airport=iad.pk,
            pickup_airline=united.pk,
            pickup_flight="123",
            dropoff="Home",
        )
    )
    assert form.is_valid(), form.errors
    pickup, dropoff = form.cleaned_data["stops"]
    assert pickup["airport_id"] == iad.pk
    assert pickup["airline_id"] == united.pk
    assert pickup["flight_number"] == "123"
    assert dropoff["airport_id"] is None and dropoff["flight_number"] == ""


def test_flight_fields_are_dropped_when_the_stop_is_not_an_airport(united):
    form = BookingRequestForm(
        _base(pickup="Home", pickup_airline=united.pk, pickup_flight="123", dropoff="Office")
    )
    assert form.is_valid(), form.errors
    pickup = form.cleaned_data["stops"][0]
    assert pickup["airline_id"] is None and pickup["flight_number"] == ""


def test_non_digit_flight_number_is_a_field_error(iad):
    form = BookingRequestForm(_base(pickup="Dulles", pickup_airport=iad.pk, pickup_flight="UA123"))
    assert not form.is_valid()
    assert "pickup_flight" in form.errors


def test_unknown_airport_is_a_field_error(db):
    form = BookingRequestForm(_base(pickup="Dulles", pickup_airport=999999))
    assert not form.is_valid()
    assert "pickup_airport" in form.errors


def test_inactive_airline_is_rejected(iad, united):
    united.is_active = False
    united.save(update_fields=["is_active"])
    form = BookingRequestForm(
        _base(pickup="Dulles", pickup_airport=iad.pk, pickup_airline=united.pk)
    )
    assert not form.is_valid()
    assert "pickup_airline" in form.errors


def test_stops_json_carries_flight_info(iad, united):
    form = BookingRequestForm(
        _base(
            pickup="Home",
            dropoff="Office",
            stops_json=json.dumps(
                [{"address": "Dulles", "airport": iad.pk, "airline": united.pk, "flight": "456"}]
            ),
        )
    )
    assert form.is_valid(), form.errors
    middle = form.cleaned_data["stops"][1]
    assert (middle["airport_id"], middle["airline_id"], middle["flight_number"]) == (
        iad.pk,
        united.pk,
        "456",
    )


def test_stops_json_rejects_a_bad_flight_number(iad):
    form = BookingRequestForm(
        _base(stops_json=json.dumps([{"address": "Dulles", "airport": iad.pk, "flight": "x"}]))
    )
    assert not form.is_valid()
    assert "stops_json" in form.errors
