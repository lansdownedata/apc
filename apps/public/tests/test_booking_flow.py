from django.test import Client


def test_booking_widget_renders_address_autocomplete(db):
    html = Client().get("/bookings/").content.decode()
    assert 'name="pickup"' in html
    assert 'name="pickup_lat"' in html
    assert 'name="dropoff"' in html
    assert "addressAutocomplete(" in html


def test_widget_has_flatpickr_phone_and_stops(db):
    html = Client().get("/bookings/").content.decode()
    assert "data-flatpickr" in html  # pickup date
    assert "data-flatpickr-time" in html  # pickup time
    assert "data-phone" in html  # phone_input component
    assert "bookingStops(" in html  # stops repeater
    assert "Special Event" not in html  # removed service


def test_full_booking_post_creates_lead_with_stops(db):
    import json

    from apps.leads.models import Lead

    resp = Client().post(
        "/bookings/",
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "passengers": 3,
            "service": "Airport Transfer",
            "pickup": "123 Main St, Ashburn VA",
            "pickup_lat": "38.9",
            "pickup_lng": "-77.4",
            "dropoff": "Dulles Intl",
            "dropoff_lat": "38.95",
            "dropoff_lng": "-77.45",
            "stops_json": json.dumps(
                [{"address": "Reston Town Center", "lat": 38.95, "lng": -77.35}]
            ),
        },
    )
    assert resp.status_code == 302
    lead = Lead.objects.latest("id")
    stops = list(lead.reservations.get().stops.order_by("sequence"))
    assert [s.address for s in stops] == [
        "123 Main St, Ashburn VA",
        "Reston Town Center",
        "Dulles Intl",
    ]
