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
    assert created == 863
    assert Airport.objects.filter(size=Airport.Size.LARGE).count() == 95
    dca = Airport.objects.get(iata="DCA")
    assert dca.name == "Ronald Reagan Washington National Airport"
    # The source sheet files Reagan National under DC, not the VA it physically sits in.
    assert dca.state == "DC"
    assert dca.city == "Washington"
