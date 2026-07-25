import json

from apps.public.forms import SERVICE_TYPE_CHOICES, BookingRequestForm


def _base(**over):
    data = {"name": "Jane Rider", "email": "jane@example.com", "passengers": 2}
    data.update(over)
    return data


def test_special_event_removed():
    assert all(v != "Special Event" for v, _ in SERVICE_TYPE_CHOICES)


def test_ordered_stops_pickup_mids_dropoff():
    form = BookingRequestForm(
        _base(
            pickup="123 Main St, Ashburn, VA",
            pickup_suite="Apt 4",
            pickup_lat="38.9",
            pickup_lng="-77.4",
            dropoff="Dulles Intl (IAD)",
            dropoff_lat="38.95",
            dropoff_lng="-77.45",
            stops_json=json.dumps(
                [{"address": "Reston Town Center", "suite": "", "lat": 38.95, "lng": -77.35}]
            ),
        )
    )
    assert form.is_valid(), form.errors
    stops = form.cleaned_data["stops"]
    assert [s["address"] for s in stops] == [
        "123 Main St, Ashburn, VA",
        "Reston Town Center",
        "Dulles Intl (IAD)",
    ]
    assert stops[0]["suite"] == "Apt 4"
    assert stops[0]["lat"] == 38.9


def test_no_addresses_yields_empty_stops():
    form = BookingRequestForm(_base())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["stops"] == []


def test_malformed_stops_json_is_rejected_not_500():
    form = BookingRequestForm(_base(stops_json="{not json"))
    assert not form.is_valid()
    assert "stops_json" in form.errors


def test_too_many_stops_rejected():
    payload = [{"address": f"Stop {i}"} for i in range(11)]
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
