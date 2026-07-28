from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirportFactory
from apps.addresses.models import Airport
from apps.integrations.geocoding import merged_autocomplete

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_airports_and_cache():
    """Migration 0003 seeds 863 real airports and the public endpoint caches on
    (q, lat, lon) for 300s — two tests below both query "BOS". Give each test an
    empty table and a cold cache."""
    Airport.objects.all().delete()
    cache.clear()
    yield
    cache.clear()


STREET = {
    "landmark_name": "",
    "line1": "14 Beacon Street",
    "line2": "",
    "city": "Boston",
    "state": "MA",
    "postal": "02108",
    "country": "US",
    "latitude": "42.3583",
    "longitude": "-71.0603",
    "place_id": "456",
    "place_type": "house",
    "place_class": "place",
    "display_name": "14 Beacon Street, Boston, MA, USA",
}
# LocationIQ's own record of the same airport — must be deduped away.
AIRPORT_TWIN = {
    "landmark_name": "Logan International Airport",
    "line1": "",
    "line2": "",
    "city": "Boston",
    "state": "MA",
    "postal": "02128",
    "country": "US",
    "latitude": "42.36197",
    "longitude": "-71.0079",
    "place_id": "123",
    "place_type": "aerodrome",
    "place_class": "aeroway",
    "display_name": "Logan International Airport, East Boston, MA, USA",
}


@pytest.fixture
def bos():
    return AirportFactory(
        ident="KBOS",
        iata="BOS",
        icao="KBOS",
        size=Airport.Size.LARGE,
        name="Boston Logan International Airport",
        city="Boston",
        state="MA",
        latitude="42.361970",
        longitude="-71.007900",
    )


def test_airports_come_first(bos):
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[STREET]):
        results = merged_autocomplete("BOS")
    assert results[0]["is_airport"] is True
    assert results[0]["airport_code"] == "BOS"
    assert results[1]["line1"] == "14 Beacon Street"


def test_locationiq_results_are_not_marked_as_airports(bos):
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[STREET]):
        results = merged_autocomplete("BOS")
    assert "is_airport" not in results[1]


def test_aeroway_duplicate_within_half_a_mile_is_dropped(bos):
    with mock.patch(
        "apps.integrations.geocoding.autocomplete", return_value=[AIRPORT_TWIN, STREET]
    ):
        results = merged_autocomplete("BOS")
    assert len(results) == 2
    assert results[0]["is_airport"] is True
    assert results[1]["line1"] == "14 Beacon Street"


def test_distant_aeroway_result_is_kept(bos):
    far = {**AIRPORT_TWIN, "latitude": "40.6413", "longitude": "-73.7781"}
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[far]):
        results = merged_autocomplete("BOS")
    assert len(results) == 2


def test_nearby_non_aeroway_result_is_kept(bos):
    near_street = {**STREET, "latitude": "42.36197", "longitude": "-71.0079"}
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[near_street]):
        results = merged_autocomplete("BOS")
    assert len(results) == 2


def test_result_with_unparseable_coordinates_is_kept(bos):
    broken = {**AIRPORT_TWIN, "latitude": None, "longitude": None}
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[broken]):
        assert len(merged_autocomplete("BOS")) == 2


def test_works_with_no_locationiq_results(bos):
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[]):
        results = merged_autocomplete("BOS")
    assert len(results) == 1
    assert results[0]["is_airport"] is True


# ---- authenticated endpoint ----


def test_auth_endpoint_returns_airports_first(client, bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    client.force_login(UserFactory())
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[STREET]):
        response = client.get(reverse("integrations:geocode_autocomplete"), {"q": "BOS"})
    body = response.json()
    assert body["degraded"] is False
    assert body["results"][0]["is_airport"] is True


def test_auth_endpoint_returns_airports_with_blank_api_key(client, bos, settings):
    settings.LOCATIONIQ_API_KEY = ""
    client.force_login(UserFactory())
    response = client.get(reverse("integrations:geocode_autocomplete"), {"q": "BOS"})
    body = response.json()
    assert body["degraded"] is True
    assert len(body["results"]) == 1
    assert body["results"][0]["airport_code"] == "BOS"


def test_auth_endpoint_still_requires_login(client, bos):
    response = client.get(reverse("integrations:geocode_autocomplete"), {"q": "BOS"})
    assert response.status_code == 302


# ---- public endpoint ----


def test_public_endpoint_returns_airports_first(client, bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    with mock.patch("apps.integrations.geocoding.autocomplete", return_value=[STREET]):
        response = client.get(reverse("public:geocode"), {"q": "BOS"})
    body = response.json()
    assert body["results"][0]["is_airport"] is True


def test_public_endpoint_returns_airports_with_blank_api_key(client, bos, settings):
    settings.LOCATIONIQ_API_KEY = ""
    response = client.get(reverse("public:geocode"), {"q": "BOS"})
    body = response.json()
    assert body["degraded"] is True
    assert body["results"][0]["airport_code"] == "BOS"


def test_public_endpoint_keeps_its_three_character_floor(client, bos, settings):
    settings.LOCATIONIQ_API_KEY = ""
    response = client.get(reverse("public:geocode"), {"q": "BO"})
    assert response.json()["results"] == []
