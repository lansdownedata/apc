import pytest
from django.test import Client


@pytest.mark.xfail(reason="widget wiring lands in the booking-widget rewrite task", strict=False)
def test_booking_widget_renders_address_autocomplete(db):
    html = Client().get("/bookings/").content.decode()
    assert 'name="pickup"' in html
    assert 'name="pickup_lat"' in html
    assert 'name="dropoff"' in html
    assert "addressAutocomplete(" in html
