from decimal import Decimal

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


def test_widget_wires_location_bias(db):
    """Pickup, drop-off, and stop rows all seed the service-area bias center and
    request the visitor's location on focus (parity with the staff smart-address)."""
    html = Client().get("/bookings/").content.decode()
    # ADDRESS_BIAS_CENTER default (DC metro) reaches the rendered widget as the fallback bias.
    assert "fallbackLat" in html
    assert "38.9531" in html  # ADDRESS_BIAS_CENTER default latitude
    # geolocation is requested on focus so suggestions bias to where the visitor is.
    assert "requestLocation()" in html
    # both the address component (pickup/drop-off) and the stops repeater carry the bias.
    assert "addressAutocomplete(" in html and "bookingStops(" in html


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


def test_widget_renders_two_segment_trip_type_toggle(db):
    html = Client().get("/bookings/").content.decode()
    assert 'name="trip_type" value="transfer"' in html
    assert 'name="trip_type" value="hourly"' in html
    # exactly two segments — no third option
    assert html.count('name="trip_type"') == 2
    assert "quoteSteps(" in html


def test_widget_renders_hours_field(db):
    html = Client().get("/bookings/").content.decode()
    assert 'name="hours"' in html
    assert 'id="bw-hours"' in html


def test_widget_renders_optional_occasion_select(db):
    html = Client().get("/bookings/").content.decode()
    assert 'name="service"' in html
    assert "Occasion" in html
    assert "Hourly Charter" not in html


def test_single_step_pages_render_every_field_at_once(db):
    """/bookings/ and /contact/ share the partial but opt out of the split.

    The fieldsets are present in the markup everywhere — the partial is not forked —
    so the contract is `twoStep: false`, which makes x-show render both, and a button
    that keeps its plain label.
    """
    for url in ("/bookings/", "/contact/"):
        html = Client().get(url).content.decode()
        assert "twoStep: false" in html, url
        assert "twoStep: true" not in html, url
        assert "Request a quote" in html, url


def test_notes_textarea_opens_at_three_rows_and_autogrows(db):
    html = Client().get("/bookings/").content.decode()
    assert 'rows="3"' in html
    assert "data-autogrow" in html


def test_home_hero_renders_two_steps(db):
    html = Client().get("/").content.decode()
    assert 'data-step="1"' in html
    assert 'data-step="2"' in html
    assert "twoStep: true" in html
    assert "data-quote-summary" in html


def test_home_hero_step_two_holds_contact_fields(db):
    """Name/email/phone/details live in step 2, trip fields in step 1."""
    html = Client().get("/").content.decode()
    step_two = html.split('data-step="2"', 1)[1]
    for field in ('name="name"', 'name="email"', 'name="phone"', 'name="notes"'):
        assert field in step_two, field
    step_one = html.split('data-step="1"', 1)[1].split('data-step="2"', 1)[0]
    for field in ('name="pickup"', 'name="dropoff"', 'name="pickup_date"', 'name="passengers"'):
        assert field in step_one, field


def test_no_js_fallback_button_label(db):
    """The button's server-rendered label works without Alpine."""
    html = Client().get("/").content.decode()
    assert "Request a quote" in html


def test_hourly_post_records_trip_type_and_hours(db):
    """Regression: create_lead_from_booking never set trip_type, so every hourly
    request the public site took was stored as a transfer."""
    from apps.leads.models import Lead

    resp = Client().post(
        "/bookings/",
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "passengers": 2,
            "trip_type": "hourly",
            "hours": "4",
            "pickup": "Dulles Intl (IAD)",
            "dropoff": "New York, NY",
        },
    )
    assert resp.status_code == 302
    res = Lead.objects.latest("id").reservations.get()
    assert res.trip_type == "hourly"
    assert res.hours == Decimal("4")


def test_transfer_post_stores_zero_hours(db):
    from apps.leads.models import Lead

    resp = Client().post(
        "/bookings/",
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "passengers": 2,
            "trip_type": "transfer",
            "hours": "4",
            "pickup": "123 Main St, Ashburn VA",
            "dropoff": "Dulles Intl",
        },
    )
    assert resp.status_code == 302
    res = Lead.objects.latest("id").reservations.get()
    assert res.trip_type == "transfer"
    assert res.hours == Decimal("0")
