from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.integrations.geocoding import autocomplete

pytestmark = pytest.mark.django_db

POI = {
    "place_id": "123",
    "lat": "42.3656",
    "lon": "-71.0096",
    "display_name": "Logan International Airport, East Boston, MA, USA",
    "class": "aeroway",
    "type": "aerodrome",
    "address": {
        "name": "Logan International Airport",
        "city": "Boston",
        "state": "Massachusetts",
        "postcode": "02128",
        "country": "United States",
    },
}
STREET = {
    "place_id": "456",
    "lat": "42.3583",
    "lon": "-71.0603",
    "display_name": "14 Beacon Street, Boston, MA, USA",
    "class": "place",
    "type": "house",
    "address": {
        "house_number": "14",
        "road": "Beacon Street",
        "city": "Boston",
        "state": "Massachusetts",
        "postcode": "02108",
        "country": "United States",
    },
}


def test_autocomplete_decomposes_a_poi_to_landmark(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        results = autocomplete("logan")
    r = results[0]
    assert r["landmark_name"] == "Logan International Airport"
    assert r["city"] == "Boston" and r["state"] == "Massachusetts" and r["postal"] == "02128"
    assert r["place_type"] == "aerodrome" and r["place_class"] == "aeroway"
    assert str(r["latitude"]) == "42.3656" and r["place_id"] == "123"


def test_autocomplete_poi_has_empty_line1(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        r = autocomplete("logan")[0]
    assert r["line1"] == ""
    assert r["landmark_name"] == "Logan International Airport"


def test_autocomplete_decomposes_a_street_to_line1_no_landmark(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [STREET]
        r = autocomplete("14 beacon")[0]
    assert r["landmark_name"] == ""
    assert r["line1"] == "14 Beacon Street"


def test_autocomplete_returns_empty_when_key_missing(settings):
    settings.LOCATIONIQ_API_KEY = ""
    assert autocomplete("anything") == []


def test_view_requires_login(client):
    resp = client.get(reverse("integrations:geocode_autocomplete"), {"q": "logan"})
    assert resp.status_code == 302


def test_view_degraded_when_key_missing(client, settings):
    settings.LOCATIONIQ_API_KEY = ""
    client.force_login(UserFactory())
    resp = client.get(reverse("integrations:geocode_autocomplete"), {"q": "logan"})
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "degraded": True}


def test_view_returns_results(client, settings):
    settings.LOCATIONIQ_API_KEY = "pk_live_SECRETKEY_ZZZ"
    client.force_login(UserFactory())
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        resp = client.get(reverse("integrations:geocode_autocomplete"), {"q": "logan"})
    body = resp.json()
    assert body["degraded"] is False
    assert body["results"][0]["landmark_name"] == "Logan International Airport"
    # the raw API key must never be echoed to the client
    assert "pk_live_SECRETKEY_ZZZ" not in resp.content.decode()


def test_autocomplete_returns_empty_on_non_list_body(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = {"error": "soft error"}  # object, not a list
        assert autocomplete("anything") == []


def test_autocomplete_skips_non_dict_items(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = ["junk", None]  # non-dict elements
        assert autocomplete("anything") == []
