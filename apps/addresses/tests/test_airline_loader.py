import pytest
from django.core.management import call_command

from apps.addresses.loaders import load_airlines
from apps.addresses.models import Airline

pytestmark = pytest.mark.django_db

HEADER = "iata,icao,name\n"
TWO_ROWS = HEADER + "UA,UAL,United Airlines\nDL,DAL,Delta Air Lines\n"


@pytest.fixture(autouse=True)
def _empty_airline_table():
    """Migration 0004 seeds the major carriers; these tests assert on exact counts."""
    Airline.objects.all().delete()


def _write(tmp_path, body):
    path = tmp_path / "airlines.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_rows_and_reports_created(tmp_path):
    created, updated = load_airlines(Airline, _write(tmp_path, TWO_ROWS))
    assert (created, updated) == (2, 0)
    assert Airline.objects.count() == 2


def test_reload_updates_in_place_keyed_on_iata(tmp_path):
    load_airlines(Airline, _write(tmp_path, TWO_ROWS))
    renamed = HEADER + "UA,UAL,United\nDL,DAL,Delta Air Lines\n"
    created, updated = load_airlines(Airline, _write(tmp_path, renamed))
    assert (created, updated) == (0, 2)
    assert Airline.objects.get(iata="UA").name == "United"


def test_codes_are_upper_cased_and_blank_rows_skipped(tmp_path):
    load_airlines(Airline, _write(tmp_path, HEADER + "ua,ual,United Airlines\n,,\n"))
    airline = Airline.objects.get()
    assert (airline.iata, airline.icao) == ("UA", "UAL")


def test_seed_airlines_command_reports_counts(tmp_path, capsys):
    call_command("seed_airlines", path=str(_write(tmp_path, TWO_ROWS)))
    assert "Airlines: 2 created, 0 updated." in capsys.readouterr().out
