import re
from decimal import Decimal
from pathlib import Path

import pytest
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
    """Pickup, drop-off, and stop rows seed the service-area bias center.

    We do NOT ask the visitor for their location. ADDRESS_BIAS_CENTER already
    describes where every trip happens, so the browser permission prompt bought
    nothing — and for a visitor browsing from outside the DMV it actively biased
    suggestions to the wrong metro.
    """
    html = Client().get("/bookings/").content.decode()
    # ADDRESS_BIAS_CENTER default (DC metro) reaches the rendered widget as the bias.
    assert "fallbackLat" in html
    assert "38.9531" in html  # ADDRESS_BIAS_CENTER default latitude
    # both the address component (pickup/drop-off) and the stops repeater carry the bias.
    assert "addressAutocomplete(" in html and "bookingStops(" in html
    # no geolocation prompt is wired to any field
    assert "requestLocation" not in html


@pytest.mark.parametrize("url", ["/", "/bookings/", "/contact/"])
def test_booking_forms_never_prompt_for_geolocation(db, url):
    html = Client().get(url).content.decode()
    assert "requestLocation" not in html
    assert "getCurrentPosition" not in html


def test_app_js_has_no_geolocation_api_calls():
    """The prompt is gone at the source, not just unwired from the templates."""
    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    assert "getCurrentPosition" not in js, (
        "app.js still calls the geolocation API — the permission prompt will fire"
    )
    assert "requestBrowserLocation" not in js, "dead geolocation helper left behind"


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


def test_thanks_page_copy(db):
    html = Client().get("/bookings/thanks/").content.decode()
    assert "Got it. We will follow up within one business day." in html
    assert "(202) 424-2600" in html


def test_autocomplete_has_combobox_semantics(db):
    html = Client().get("/bookings/").content.decode()
    assert 'role="combobox"' in html
    assert ':aria-expanded="open"' in html
    assert "aria-controls=" in html
    assert 'aria-live="polite"' in html


def test_autocomplete_debounce_is_250ms(db):
    html = Client().get("/bookings/").content.decode()
    assert "debounce.250ms" in html
    assert "debounce.300ms" not in html


def test_stop_rows_support_arrow_keys(db):
    """Stop rows previously handled only Escape.

    Asserts the row-indexed handlers specifically — the bare `keydown.down.prevent`
    string is already present via the pickup/drop-off inputs, so checking for it
    alone would pass without the stop rows changing at all.
    """
    html = Client().get("/bookings/").content.decode()
    assert 'keydown.down.prevent="move(i, 1)"' in html
    assert 'keydown.up.prevent="move(i, -1)"' in html
    assert 'keydown.enter.prevent="chooseActive(i)"' in html


def test_result_rows_are_listbox_options(db):
    html = Client().get("/bookings/").content.decode()
    assert 'role="listbox"' in html
    assert 'role="option"' in html


def test_notes_textarea_opens_at_three_rows_and_autogrows(db):
    html = Client().get("/bookings/").content.decode()
    assert 'rows="3"' in html
    assert "data-autogrow" in html


def test_autogrow_never_sizes_a_textarea_that_has_no_layout():
    """The homepage hero hides step 2 behind x-show, and Alpine (deferred) applies
    that before app.js runs its DOMContentLoaded pass. A display:none textarea
    reports scrollHeight 0, so sizing it collapsed "Trip details" to its padding —
    the clipped box — and nothing re-measured it once the step was revealed.

    So the sizer must bail while the field has no layout, and something must
    re-measure when it gains some.
    """
    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    grow = re.search(r"const grow = \(\) => \{(.+?)\n    \};", js, re.S)
    assert grow, "no grow() in initAutogrow"
    assert "offsetParent" in grow.group(1), (
        f"grow() sizes the textarea without checking it is rendered: {grow.group(1)!r}"
    )
    assert "ResizeObserver" in js, (
        "nothing re-measures the textarea when its step is revealed, so it stays collapsed"
    )


def _order(html, *needles):
    """Positions of each needle, asserting all are present."""
    idx = []
    for n in needles:
        i = html.find(n)
        assert i != -1, f"missing from markup: {n}"
        idx.append(i)
    return idx


def test_add_a_stop_sits_between_pickup_and_dropoff(db):
    """A stop is a waypoint *on the way*, so the control belongs between the two
    address fields. It previously lived in the contact fieldset, which pushed it
    below Passengers on the full form and dropped it from the hero entirely.
    """
    for url in ("/", "/bookings/", "/contact/"):
        html = Client().get(url).content.decode()
        pickup, add_stop, dropoff = _order(html, 'id="bw-pickup"', "Add a stop", 'id="bw-dropoff"')
        assert pickup < add_stop < dropoff, (
            f"{url}: expected pickup < 'Add a stop' < drop-off, got {pickup}, {add_stop}, {dropoff}"
        )


def test_add_a_stop_shares_the_dropoff_label_row(db):
    """It rides the Drop-off label row rather than taking a row of its own.

    A standalone row broke the label→field rhythm the rest of the card keeps, so
    the gap above Drop-off read wider than every other gap. Encoded as ordering:
    the label text, then the button, then the input it labels.
    """
    for url in ("/", "/bookings/", "/contact/"):
        html = Client().get(url).content.decode()
        label, add_stop, field = _order(html, "Drop-off location", "Add a stop", 'id="bw-dropoff"')
        assert label < add_stop < field, (
            f"{url}: 'Add a stop' is not on the drop-off label row ({label}, {add_stop}, {field})"
        )


def test_hero_step_one_holds_the_stops_repeater(db):
    """The hero's short form must be able to add stops without reaching step 2."""
    html = Client().get("/").content.decode()
    step_one = html.split('data-step="1"', 1)[1].split('data-step="2"', 1)[0]
    assert "Add a stop" in step_one
    assert "bookingStops(" in step_one
    assert 'name="stops_json"' in step_one


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


def test_full_booking_post_stores_the_pickup_flight(db):
    from apps.addresses.models import Airline, Airport
    from apps.leads.models import Lead

    iad = Airport.objects.get(iata="IAD")  # seeded by addresses.0003
    united = Airline.objects.get(iata="UA")
    resp = Client().post(
        "/bookings/",
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "passengers": 1,
            "pickup": "Dulles Intl",
            "pickup_airport": iad.pk,
            "pickup_airline": united.pk,
            "pickup_flight": "123",
            "dropoff": "123 Main St, Ashburn VA",
        },
    )
    assert resp.status_code == 302
    stop = Lead.objects.latest("id").reservations.get().stops.order_by("sequence").first()
    assert (stop.airport, stop.airline, stop.flight_number) == (iad, united, "123")


def test_widget_shows_an_unknown_airport_error(db):
    resp = Client().post(
        "/bookings/",
        {
            "name": "Jane Rider",
            "email": "jane@example.com",
            "passengers": 1,
            "pickup": "Dulles",
            "pickup_airport": 999999,
            "dropoff": "Home",
        },
    )
    assert resp.status_code == 200
    assert "Unknown airport." in resp.content.decode()


def test_widget_renders_flight_fields_for_pickup_and_dropoff(db):
    html = Client().get("/bookings/").content.decode()
    for name in ("pickup", "dropoff"):
        assert f'name="{name}_airport"' in html
        assert f'name="{name}_airline"' in html
        assert f'name="{name}_flight"' in html
    assert "UA — United Airlines</option>" in html
    assert 'x-model="stop.flight"' in html  # the in-between stop repeater
    assert "flightVerifyComingSoon" not in html  # Verify is staff-only
