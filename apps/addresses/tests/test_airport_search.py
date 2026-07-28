import pytest

from apps.addresses.factories import AirportFactory
from apps.addresses.models import Airport
from apps.addresses.search import search_airports
from apps.integrations.geocoding import _decompose

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _empty_airport_table():
    """Migration 0003 seeds all 863 real airports. These tests assert on exact result
    sets, so they need a table containing only what each test creates."""
    Airport.objects.all().delete()


@pytest.fixture
def dca():
    return AirportFactory(
        ident="KDCA",
        iata="DCA",
        icao="KDCA",
        size=Airport.Size.LARGE,
        name="Ronald Reagan Washington National Airport",
        city="Washington",
        state="DC",
        latitude="38.852083",
        longitude="-77.037722",
    )


@pytest.fixture
def reef():
    return AirportFactory(
        ident="07FA",
        iata="07FA",
        icao="",
        size=Airport.Size.MEDIUM,
        name="Ocean Reef Club Airport",
        city="Key Largo",
        state="FL",
        latitude="25.325399",
        longitude="-80.274803",
    )


def _codes(results):
    return [r["airport_code"] for r in results]


# ---- minimum query length ----


def test_query_shorter_than_two_chars_returns_nothing(dca):
    assert search_airports("D") == []
    assert search_airports("") == []
    assert search_airports("  ") == []


# ---- exact code matching ----


def test_matches_iata_exactly(dca):
    assert _codes(search_airports("DCA")) == ["DCA"]


def test_code_match_is_case_insensitive(dca):
    assert _codes(search_airports("dca")) == ["DCA"]


def test_matches_icao_exactly(dca):
    assert _codes(search_airports("KDCA")) == ["DCA"]


def test_matches_ident_exactly_on_a_medium_airport_at_four_chars(reef):
    assert _codes(search_airports("07FA")) == ["07FA"]


def test_exact_code_match_ignores_the_medium_gate():
    medium = AirportFactory(
        ident="K67L",
        iata="67L",
        size=Airport.Size.MEDIUM,
        name="Mesquite Airport",
        city="Mesquite",
        state="NV",
    )
    assert _codes(search_airports("67L")) == [medium.iata]


# ---- name / city word-prefix matching ----


def test_matches_name_from_the_first_word(dca):
    assert _codes(search_airports("Ronald")) == ["DCA"]


def test_matches_a_word_after_a_space(dca):
    assert _codes(search_airports("Reagan")) == ["DCA"]


def test_matches_a_word_after_a_hyphen():
    AirportFactory(
        iata="BHM",
        name="Birmingham-Shuttlesworth International Airport",
        city="Birmingham",
        state="AL",
        size=Airport.Size.LARGE,
    )
    assert _codes(search_airports("Shuttlesworth")) == ["BHM"]


def test_matches_a_city_word_after_a_slash():
    AirportFactory(
        iata="ABE",
        name="Lehigh Valley International Airport",
        city="Allentown/Bethlehem",
        state="PA",
        size=Airport.Size.LARGE,
    )
    assert _codes(search_airports("Bethlehem")) == ["ABE"]


def test_does_not_match_mid_word(dca):
    assert search_airports("eagan") == []


def test_matches_city(dca):
    assert _codes(search_airports("Washington")) == ["DCA"]


# ---- the medium gate ----


def test_medium_airport_hidden_from_name_match_below_four_chars():
    AirportFactory(
        iata="SPI",
        name="Springfield Airport",
        city="Springfield",
        state="IL",
        size=Airport.Size.MEDIUM,
    )
    assert search_airports("Spr") == []


def test_medium_airport_appears_at_four_chars():
    AirportFactory(
        iata="SPI",
        name="Springfield Airport",
        city="Springfield",
        state="IL",
        size=Airport.Size.MEDIUM,
    )
    assert _codes(search_airports("Spri")) == ["SPI"]


def test_large_airport_appears_below_four_chars(dca):
    assert _codes(search_airports("Rea")) == ["DCA"]


# ---- ordering and limit ----


def test_exact_code_ranks_above_a_name_match():
    AirportFactory(
        iata="BOS",
        name="Boston Logan International Airport",
        city="Boston",
        state="MA",
        size=Airport.Size.LARGE,
    )
    AirportFactory(
        iata="XYZ",
        name="BOS Memorial Airport",
        city="Elsewhere",
        state="TX",
        size=Airport.Size.LARGE,
    )
    assert _codes(search_airports("BOS"))[0] == "BOS"


def test_large_ranks_above_medium():
    AirportFactory(
        iata="MED",
        name="Riverside Municipal Airport",
        city="Riverside",
        state="CA",
        size=Airport.Size.MEDIUM,
    )
    AirportFactory(
        iata="LRG",
        name="Riverside International Airport",
        city="Riverside",
        state="CA",
        size=Airport.Size.LARGE,
    )
    assert _codes(search_airports("Riverside")) == ["LRG", "MED"]


def test_capped_at_three_results():
    for n in range(5):
        AirportFactory(
            name=f"Portland Field {n}", city="Portland", state="OR", size=Airport.Size.LARGE
        )
    assert len(search_airports("Portland")) == 3


def test_limit_is_overridable():
    for n in range(5):
        AirportFactory(
            name=f"Portland Field {n}", city="Portland", state="OR", size=Airport.Size.LARGE
        )
    assert len(search_airports("Portland", limit=5)) == 5


def test_inactive_airports_excluded(dca):
    Airport.objects.filter(pk=dca.pk).update(is_active=False)
    assert search_airports("DCA") == []


# ---- payload shape ----


def test_payload_keys_match_decompose_exactly(dca):
    locationiq_keys = set(_decompose({"address": {}}))
    payload_keys = set(search_airports("DCA")[0])
    assert payload_keys == locationiq_keys | {"is_airport", "airport_code"}


def test_payload_values(dca):
    r = search_airports("DCA")[0]
    assert r["landmark_name"] == "Ronald Reagan Washington National Airport"
    assert r["city"] == "Washington"
    assert r["state"] == "DC"
    assert r["country"] == "US"
    assert r["place_class"] == "aeroway"
    assert r["place_type"] == "aerodrome"
    assert r["is_airport"] is True
    assert r["line2"] == ""
    assert float(r["latitude"]) == pytest.approx(38.852083)
    assert float(r["longitude"]) == pytest.approx(-77.037722)


def test_display_name_synthesized_when_not_enriched(dca):
    r = search_airports("DCA")[0]
    assert r["display_name"] == "Ronald Reagan Washington National Airport, Washington, DC"


def test_enrichment_fills_line1_postal_place_id_and_display_name(dca):
    Airport.objects.filter(pk=dca.pk).update(
        line1="2401 Smith Blvd",
        postal="20001",
        locationiq_place_id="abc123",
        display_name="Reagan National, Arlington, VA, USA",
    )
    r = search_airports("DCA")[0]
    assert r["line1"] == "2401 Smith Blvd"
    assert r["postal"] == "20001"
    assert r["place_id"] == "abc123"
    assert r["display_name"] == "Reagan National, Arlington, VA, USA"


def test_coordinates_always_come_from_the_row_not_enrichment(dca):
    Airport.objects.filter(pk=dca.pk).update(display_name="somewhere else entirely")
    r = search_airports("DCA")[0]
    assert float(r["latitude"]) == pytest.approx(38.852083)
