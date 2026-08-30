import pytest
from django.utils import timezone

from apps.addresses.loaders import CSV_PATH, load_airports
from apps.addresses.models import Airport

pytestmark = pytest.mark.django_db

HEADER = (
    "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,"
    "country,city,icao_code,iata_code,State\n"
)
TWO_ROWS = HEADER + (
    "1,KDCA,large_airport,Ronald Reagan Washington National Airport,"
    "38.852083,-77.037722,15,US,Washington,KDCA,DCA,VA\n"
    "2,07FA,medium_airport,Ocean Reef Club Airport,25.325399,-80.274803,"
    ",US,Key Largo,,07FA,FL\n"
)


@pytest.fixture(autouse=True)
def _empty_airport_table():
    """Migration 0003 seeds all 863 airports, so the table is never empty by default.
    These tests assert on exact counts — give each one a table it fully owns."""
    Airport.objects.all().delete()


def _write(tmp_path, body):
    path = tmp_path / "airports.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_rows_and_reports_created(tmp_path):
    created, updated = load_airports(Airport, _write(tmp_path, TWO_ROWS))
    assert (created, updated) == (2, 0)
    assert Airport.objects.count() == 2


def test_maps_columns_onto_fields(tmp_path):
    load_airports(Airport, _write(tmp_path, TWO_ROWS))
    dca = Airport.objects.get(ident="KDCA")
    assert dca.iata == "DCA"
    assert dca.icao == "KDCA"
    assert dca.size == Airport.Size.LARGE
    assert dca.city == "Washington"
    assert dca.state == "VA"
    assert dca.ourairports_id == 1
    assert float(dca.latitude) == pytest.approx(38.852083)
    assert dca.elevation_ft == 15


def test_blank_icao_and_elevation_tolerated(tmp_path):
    load_airports(Airport, _write(tmp_path, TWO_ROWS))
    reef = Airport.objects.get(ident="07FA")
    assert reef.icao == ""
    assert reef.elevation_ft is None


def test_reload_is_idempotent(tmp_path):
    path = _write(tmp_path, TWO_ROWS)
    load_airports(Airport, path)
    created, updated = load_airports(Airport, path)
    assert (created, updated) == (0, 2)
    assert Airport.objects.count() == 2


def test_reload_preserves_enrichment(tmp_path):
    path = _write(tmp_path, TWO_ROWS)
    load_airports(Airport, path)
    Airport.objects.filter(ident="KDCA").update(
        locationiq_place_id="abc123",
        line1="2401 Smith Blvd",
        postal="20001",
        enriched_at=timezone.now(),
    )
    load_airports(Airport, path)
    dca = Airport.objects.get(ident="KDCA")
    assert dca.locationiq_place_id == "abc123"
    assert dca.line1 == "2401 Smith Blvd"
    assert dca.enriched_at is not None


def test_committed_csv_loads_completely():
    created, _ = load_airports(Airport, CSV_PATH)
    # 863 curated US ground-transport airports + the global scheduled-service large/medium
    # set (2026-08-29 flight-verification data expansion, see the airport-data report).
    assert created == 3637
    assert Airport.objects.filter(size=Airport.Size.LARGE).count() == 1147
    dca = Airport.objects.get(iata="DCA")
    assert dca.name == "Ronald Reagan Washington National Airport"
    # The source sheet files Reagan National under DC, not the VA it physically sits in.
    assert dca.state == "DC"
    assert dca.city == "Washington"


TZ_HEADER = HEADER.replace("State\n", "State,timezone\n")
TZ_ROWS = TZ_HEADER + (
    "1,KDCA,large_airport,Ronald Reagan Washington National Airport,"
    "38.852083,-77.037722,15,US,Washington,KDCA,DCA,VA,America/New_York\n"
    "3,KLAX,large_airport,Los Angeles International Airport,33.942501,-118.407997,"
    "125,US,Los Angeles,KLAX,LAX,CA,America/Los_Angeles\n"
)


def test_timezone_column_is_loaded(tmp_path):
    load_airports(Airport, _write(tmp_path, TZ_ROWS))
    assert Airport.objects.get(ident="KDCA").timezone == "America/New_York"
    assert Airport.objects.get(ident="KLAX").timezone == "America/Los_Angeles"


def test_a_csv_without_the_timezone_column_still_loads(tmp_path):
    load_airports(Airport, _write(tmp_path, TWO_ROWS))
    assert Airport.objects.get(ident="KDCA").timezone == ""


def test_committed_csv_has_a_valid_timezone_for_every_row():
    import csv
    from zoneinfo import ZoneInfo

    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and "timezone" in rows[0]
    missing = [r["ident"] for r in rows if not (r.get("timezone") or "").strip()]
    assert missing == []
    for r in rows:
        ZoneInfo(r["timezone"])  # raises for anything that isn't a real IANA name


# --- ground-transport eligibility / scheduled-service columns (2026-08-29) -----------

GT_HEADER = HEADER.replace("State\n", "State,serves_ground_transport,has_scheduled_service\n")
GT_ROWS = GT_HEADER + (
    "1,KDCA,large_airport,Ronald Reagan Washington National Airport,"
    "38.852083,-77.037722,15,US,Washington,KDCA,DCA,VA,true,true\n"
    "2,EGLL,large_airport,London Heathrow Airport,"
    "51.470748,-0.459909,83,GB,London,EGLL,LHR,,false,true\n"
    "3,KADW,medium_airport,Joint Base Andrews,"
    "38.810799,-76.866997,280,US,Camp Springs,KADW,ADW,MD,true,false\n"
)


def test_ground_transport_and_scheduled_service_columns_are_loaded(tmp_path):
    load_airports(Airport, _write(tmp_path, GT_ROWS))
    dca = Airport.objects.get(ident="KDCA")
    assert (dca.serves_ground_transport, dca.has_scheduled_service) == (True, True)
    lhr = Airport.objects.get(ident="EGLL")
    assert (lhr.serves_ground_transport, lhr.has_scheduled_service) == (False, True)
    adw = Airport.objects.get(ident="KADW")
    assert (adw.serves_ground_transport, adw.has_scheduled_service) == (True, False)


def test_a_csv_without_the_new_flag_columns_still_loads_and_defaults_both_false(tmp_path):
    """Old fixtures (TWO_ROWS) predate these columns — a missing column must not 400/crash
    the loader, and must not silently grant ground-transport or Verify eligibility."""
    load_airports(Airport, _write(tmp_path, TWO_ROWS))
    dca = Airport.objects.get(ident="KDCA")
    assert (dca.serves_ground_transport, dca.has_scheduled_service) == (False, False)


def test_committed_csv_flags_are_unambiguous_for_every_row():
    import csv

    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for col in ("serves_ground_transport", "has_scheduled_service"):
        bad = [
            r["ident"] for r in rows if (r.get(col) or "").strip().lower() not in ("true", "false")
        ]
        assert bad == [], f"{col} is missing/ambiguous for {bad[:5]}"


def test_committed_csv_matches_the_three_findings():
    """Ground truth for the three live-testing findings this data expansion fixes."""
    created, _ = load_airports(Airport, CSV_PATH)
    assert created == 3637
    lhr = Airport.objects.get(iata="LHR")
    assert (lhr.serves_ground_transport, lhr.has_scheduled_service) == (False, True)
    sju = Airport.objects.get(iata="SJU")
    assert (sju.serves_ground_transport, sju.has_scheduled_service) == (True, True)
    adw = Airport.objects.get(iata="ADW")
    assert (adw.serves_ground_transport, adw.has_scheduled_service) == (True, False)
    # 863 curated + 11 US territories
    assert Airport.objects.filter(serves_ground_transport=True).count() == 874
    assert Airport.objects.filter(has_scheduled_service=True).count() == 3246
    assert (
        Airport.objects.filter(serves_ground_transport=True, has_scheduled_service=False).count()
        == 391
    )
