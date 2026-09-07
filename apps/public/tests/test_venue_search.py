"""The wedding intake's venue / hotel / ceremony-site typeahead."""

import pytest
from django.core.cache import cache

from apps.addresses.factories import VenueFactory
from apps.addresses.models import Venue
from apps.addresses.search import VENUE_RESULT_LIMIT

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


def test_kind_filter_excludes_other_kinds(client, monkeypatch):
    """`kind` narrows the directory half. LocationIQ rows have never carried a kind."""
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete", lambda q, lat=None, lon=None: []
    )
    rows = client.get(URL, {"q": "lansdowne", "kind": "hotel"}).json()["results"]
    assert "Lansdowne Resort" in [r["name"] for r in rows]
    assert {r["kind"] for r in rows if r["source"] == "directory"} == {"hotel"}


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


def test_a_strong_directory_match_set_never_triggers_a_locationiq_call(client, monkeypatch):
    """Plenty of name matches on file — no reason to spend a paid lookup."""

    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("LocationIQ called although the directory matched well")

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", boom)
    # "manor" hits four+ directory rows by name.
    assert len(client.get(URL, {"q": "manor"}).json()["results"]) >= 4


def test_merges_locationiq_rows_when_the_directory_match_is_thin(client, monkeypatch):
    """A single weak directory hit no longer hides real venues (reconciliation §A1)."""
    VenueFactory(name="Rosewood Chapel", city="Vienna", state="VA", lead_hits=5)
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": "Rosewood Manor",
                "line1": "9 Rose Ln",
                "city": "Fairfax",
                "state": "VA",
                "latitude": "38.8",
                "longitude": "-77.3",
                "display_name": "Rosewood Manor, Fairfax, VA",
            }
        ],
    )
    results = client.get(URL, {"q": "rosewood"}).json()["results"]
    names = [r["name"] for r in results]
    assert names[0] == "Rosewood Chapel"  # directory rows come first
    assert results[0]["source"] == "directory"
    assert "Rosewood Manor" in names  # LocationIQ row appended
    assert any(r["source"] == "locationiq" for r in results)


def test_a_locationiq_row_duplicating_a_directory_row_is_dropped(client, monkeypatch):
    # "Airlie" is a seeded directory row; LocationIQ returning it too must not double it.
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": "AIRLIE",
                "line1": "6809 Airlie Rd",
                "city": "warrenton",
                "state": "VA",
                "latitude": "38.7",
                "longitude": "-77.7",
                "display_name": "Airlie, Warrenton, VA",
            }
        ],
    )
    results = client.get(URL, {"q": "airlie"}).json()["results"]
    assert [r["name"].lower() for r in results].count("airlie") == 1
    assert results[0]["source"] == "directory"


def test_merged_results_stay_capped_at_the_limit(client, monkeypatch):
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": f"Barn {i}",
                "line1": f"{i} Rd",
                "city": "Hume",
                "state": "VA",
                "latitude": "38.6",
                "longitude": "-78.0",
                "display_name": f"Barn {i}, Hume, VA",
            }
            for i in range(20)
        ],
    )
    results = client.get(URL, {"q": "barn"}).json()["results"]
    assert len(results) <= VENUE_RESULT_LIMIT


def test_merge_preserves_the_degraded_flag_and_throttle(client, monkeypatch, settings):
    from apps.public.views import GEOCODE_THROTTLE_LIMIT

    settings.LOCATIONIQ_API_KEY = ""
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete", lambda q, lat=None, lon=None: []
    )
    resp = client.get(URL, {"q": "congress"})
    assert resp.json()["degraded"] is True
    for _ in range(GEOCODE_THROTTLE_LIMIT):
        client.get(URL, {"q": "congress"})
    assert client.get(URL, {"q": "congress"}).status_code == 429


def test_a_thin_hotel_match_still_reaches_locationiq(client, monkeypatch):
    """A kinded field (hotel / ceremony site) gets the same merge as the venue field.

    The couple's room block is almost never a venue we curate. One weak curated hit
    ("Hom" -> Homewood Suites Leesburg) must not hide the Home2 they actually booked.
    """
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": "Home2 Suites Dulles",
                "line1": "45520 E Severn Way",
                "city": "Sterling",
                "state": "VA",
                "latitude": "39.0",
                "longitude": "-77.4",
                "place_id": "3210",
                "display_name": "Home2 Suites Dulles, Sterling, VA",
            }
        ],
    )
    results = client.get(URL, {"q": "hom", "kind": "hotel"}).json()["results"]
    names = [r["name"] for r in results]
    assert names[0] == "Homewood Suites Leesburg"  # directory rows still come first
    assert "Home2 Suites Dulles" in names


def test_a_kinded_field_with_plenty_of_matches_spends_no_lookup(client, monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("LocationIQ called although the directory matched well")

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", boom)
    assert client.get(URL, {"q": "hilton", "kind": "hotel"}).json()["results"]


def test_locationiq_rows_carry_a_unique_key_for_the_typeahead(client, monkeypatch):
    """Two same-named hotels in different towns are two rows, not one duplicate key.

    `id` is None on every LocationIQ row, so the typeahead keys on `place_id`;
    duplicate keys wedge the Alpine `x-for` and the dropdown stops rendering.
    """
    monkeypatch.setattr(
        "apps.public.views.locationiq_autocomplete",
        lambda q, lat=None, lon=None: [
            {
                "landmark_name": "Hampton Inn",
                "line1": "1 A St",
                "city": "Sterling",
                "state": "VA",
                "place_id": "111",
                "display_name": "Hampton Inn, Sterling, VA",
            },
            {
                "landmark_name": "Hampton Inn",
                "line1": "2 B St",
                "city": "Manassas",
                "state": "VA",
                "place_id": "222",
                "display_name": "Hampton Inn, Manassas, VA",
            },
        ],
    )
    rows = [
        r
        for r in client.get(URL, {"q": "hampton inn", "kind": "hotel"}).json()["results"]
        if r["source"] == "locationiq"
    ]
    assert len(rows) == 2
    keys = [r["id"] or r["place_id"] for r in rows]
    assert len(set(keys)) == len(keys)


def test_locationiq_results_are_biased_to_the_service_area(client, monkeypatch, settings):
    """ "Hom" must not offer Homs, Syria to a couple marrying in Loudoun.

    The address typeahead already sends a soft viewbox around ADDRESS_BIAS_CENTER; the
    venue typeahead searches the same world and needs the same bias.
    """
    settings.ADDRESS_BIAS_CENTER = (38.9531, -77.4565)
    seen = {}

    def spy(q, lat=None, lon=None):
        seen.update(q=q, lat=lat, lon=lon)
        return []

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", spy)
    client.get(URL, {"q": "hom", "kind": "hotel"})
    assert (seen["lat"], seen["lon"]) == (38.9531, -77.4565)


def test_locationiq_rows_are_ranked_by_distance_from_the_service_area(
    client, monkeypatch, settings
):
    """LocationIQ ignores the viewbox for ranking, so we rank its rows ourselves.

    Probed 2026-09-06: "Red Roof Inn Leesburg Va" comes back with Marion, Chesapeake and
    Virginia Beach ahead of Fredericksburg — identical order with and without the
    viewbox. Keeping the API's first eight showed a Loudoun couple nothing within
    200 miles.
    """
    settings.ADDRESS_BIAS_CENTER = (38.9531, -77.4565)

    def rows(q, lat=None, lon=None):
        return [
            {
                "landmark_name": "Red Roof Inn Marion",
                "line1": "1424 N Main St",
                "city": "Marion",
                "state": "VA",
                "latitude": "36.845",
                "longitude": "-81.492",
                "place_id": "1",
            },
            {
                "landmark_name": "Red Roof Inn Fredericksburg",
                "line1": "564 Warrenton Rd",
                "city": "Fredericksburg",
                "state": "VA",
                "latitude": "38.338",
                "longitude": "-77.488",
                "place_id": "2",
            },
        ]

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", rows)
    resp = client.get(URL, {"q": "red roof inn", "kind": "hotel"})
    names = [r["name"] for r in resp.json()["results"]]
    assert names[0] == "Red Roof Inn Fredericksburg"


def test_a_locationiq_row_without_coordinates_sorts_last_rather_than_erroring(
    client, monkeypatch, settings
):
    settings.ADDRESS_BIAS_CENTER = (38.9531, -77.4565)

    def rows(q, lat=None, lon=None):
        return [
            {"landmark_name": "No Coords Inn", "city": "Nowhere", "state": "VA", "place_id": "1"},
            {
                "landmark_name": "Ashburn Inn",
                "city": "Ashburn",
                "state": "VA",
                "latitude": "39.04",
                "longitude": "-77.48",
                "place_id": "2",
            },
        ]

    monkeypatch.setattr("apps.public.views.locationiq_autocomplete", rows)
    resp = client.get(URL, {"q": "zzz lodging", "kind": "hotel"})
    names = [r["name"] for r in resp.json()["results"]]
    assert names.index("Ashburn Inn") < names.index("No Coords Inn")
