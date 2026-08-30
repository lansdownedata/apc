"""The wedding intake's venue / hotel / ceremony-site typeahead."""

import pytest
from django.core.cache import cache

from apps.addresses.factories import VenueFactory
from apps.addresses.models import Venue

pytestmark = pytest.mark.django_db

URL = "/weddings/venues/"


@pytest.fixture(autouse=True)
def _clear_throttle():
    cache.clear()


def test_finds_a_seeded_venue_with_its_cap(client):
    data = client.get(URL, {"q": "oak"}).json()
    oak = next(r for r in data["results"] if r["name"] == "The Oak Barn at Loyalty")
    assert oak["vehicle_cap"] == 40
    assert oak["cap_note"]
    assert oak["id"]


def test_results_are_ordered_by_lead_hits_then_name(client):
    Venue.objects.all().delete()
    VenueFactory(name="Zulu Manor", lead_hits=30)
    VenueFactory(name="Alpha Manor", lead_hits=2)
    VenueFactory(name="Beta Manor", lead_hits=30)
    names = [r["name"] for r in client.get(URL, {"q": "manor"}).json()["results"]]
    assert names == ["Beta Manor", "Zulu Manor", "Alpha Manor"]


def test_kind_filter_excludes_other_kinds(client):
    names = [
        r["name"] for r in client.get(URL, {"q": "lansdowne", "kind": "hotel"}).json()["results"]
    ]
    kinds = {
        r["kind"] for r in client.get(URL, {"q": "lansdowne", "kind": "hotel"}).json()["results"]
    }
    assert "Lansdowne Resort" in names
    assert kinds == {"hotel"}


def test_an_unknown_kind_is_ignored_rather_than_erroring(client):
    resp = client.get(URL, {"q": "oak", "kind": "spaceship"})
    assert resp.status_code == 200
    assert resp.json()["results"]


def test_city_matches_too(client):
    names = [r["name"] for r in client.get(URL, {"q": "purcellville"}).json()["results"]]
    assert "Breaux Vineyards" in names


def test_inactive_venues_are_hidden(client):
    Venue.objects.filter(name="The Oak Barn at Loyalty").update(is_active=False)
    names = [r["name"] for r in client.get(URL, {"q": "oak"}).json()["results"]]
    assert "The Oak Barn at Loyalty" not in names


def test_short_queries_return_nothing_without_touching_the_directory(client):
    assert client.get(URL, {"q": "o"}).json()["results"] == []


def test_results_are_capped_at_eight(client):
    assert len(client.get(URL, {"q": "e"}).json()["results"]) <= 8


def test_throttle_mirrors_the_geocode_proxy(client):
    from apps.public.views import GEOCODE_THROTTLE_LIMIT

    for _ in range(GEOCODE_THROTTLE_LIMIT):
        assert client.get(URL, {"q": "oak"}).status_code == 200
    assert client.get(URL, {"q": "oak"}).status_code == 429


def test_the_venue_throttle_does_not_spend_the_address_proxys_budget(client):
    """Two separate public autocompletes; one must not lock out the other."""
    for _ in range(10):
        client.get(URL, {"q": "oak"})
    assert client.get("/bookings/geocode/", {"q": "leesburg"}).status_code == 200


def test_falls_through_to_locationiq_when_the_directory_has_no_match(client, monkeypatch):
    """An unknown venue still resolves to an address rather than a dead end."""
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": "Willow Oaks Barn",
                "line1": "1 Willow Rd",
                "city": "Berryville",
                "state": "VA",
                "latitude": "39.15",
                "longitude": "-77.98",
                "display_name": "Willow Oaks Barn, Berryville, VA",
            }
        ],
    )
    results = client.get(URL, {"q": "willow oaks barn"}).json()["results"]
    assert results[0]["name"] == "Willow Oaks Barn"
    assert results[0]["id"] is None
    assert results[0]["source"] == "locationiq"
    assert results[0]["latitude"] == "39.15"


def test_directory_matches_never_trigger_a_locationiq_call(client, monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("LocationIQ called although the directory matched")

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", boom)
    assert client.get(URL, {"q": "oak"}).json()["results"]
