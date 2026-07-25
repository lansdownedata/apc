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


def _params_of(mock_get):
    return mock_get.call_args.kwargs["params"]


def test_autocomplete_adds_soft_viewbox_when_biased(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    settings.ADDRESS_BIAS_RADIUS_DEG = 0.75
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        autocomplete("dulles", lat=38.95, lon=-77.45)
    p = _params_of(g)
    assert "viewbox" in p and "bounded" not in p
    # box built around the center: "<lon-R>,<lat+R>,<lon+R>,<lat-R>"
    assert p["viewbox"] == "-78.2,39.7,-76.7,38.2"
    assert p["limit"] == 20 and p["dedupe"] == 1 and p["normalizecity"] == 1
    for forbidden in ("countrycodes", "bounded", "importancesort"):
        assert forbidden not in p


def test_autocomplete_no_viewbox_without_coords(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        autocomplete("dulles")
    assert "viewbox" not in _params_of(g)


def test_autocomplete_bad_coords_skip_viewbox(settings):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        autocomplete("dulles", lat="abc", lon="def")  # never raises, no viewbox
    assert "viewbox" not in _params_of(g)


def test_view_forwards_coords(client, settings):
    settings.LOCATIONIQ_API_KEY = "k"
    client.force_login(UserFactory())
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [POI]
        client.get(
            reverse("integrations:geocode_autocomplete"),
            {"q": "dulles", "lat": "38.95", "lon": "-77.45"},
        )
    assert "viewbox" in g.call_args.kwargs["params"]


STREET_HW = {
    "place_id": "321234735530",
    "lat": "39.1034772",
    "lon": "-77.5218607",
    "class": "highway",
    "type": "residential",
    "display_name": (
        "Big Spruce Square, Spring Lake, Leesburg, Loudoun County, Virginia, 20176, USA"
    ),
    "address": {
        "name": "Big Spruce Square",
        "house_number": "42748",
        "road": "Big Spruce Square",
        "city": "Leesburg",
        "state": "Virginia",
        "state_code": "va",
        "postcode": "20176",
        "country": "United States of America",
        "country_code": "us",
    },
}


def _one(settings, fixture, q="x"):
    settings.LOCATIONIQ_API_KEY = "k"
    with mock.patch("apps.integrations.geocoding.requests.get") as g:
        g.return_value.status_code = 200
        g.return_value.json.return_value = [fixture]
        return autocomplete(q)[0]


def test_decompose_uses_state_and_country_codes(settings):
    r = _one(settings, STREET_HW)
    assert r["state"] == "VA"
    assert r["country"] == "US"


def test_decompose_landmark_blank_for_a_street(settings):
    r = _one(settings, STREET_HW)
    assert r["landmark_name"] == ""  # class=highway → not a POI
    assert r["line1"] == "42748 Big Spruce Square"  # street still lands in line1


def test_decompose_landmark_kept_for_a_poi(settings):
    r = _one(settings, POI)  # POI fixture is class=aeroway
    assert r["landmark_name"] == "Logan International Airport"


def test_decompose_falls_back_to_full_state_without_code(settings):
    item = {**STREET_HW, "address": {**STREET_HW["address"]}}
    del item["address"]["state_code"]
    del item["address"]["country_code"]
    r = _one(settings, item)
    assert r["state"] == "Virginia"  # no code → full name unchanged
    assert r["country"] == "United States of America"
