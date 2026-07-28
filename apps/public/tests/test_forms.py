import json
from decimal import Decimal

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
