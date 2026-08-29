import pytest

from apps.addresses.models import Airline

pytestmark = pytest.mark.django_db


def test_migration_seeds_the_major_carriers():
    """The committed CSV is loaded by migration 0004, so a fresh database can attach a
    flight to a stop without anyone running a command first."""
    import csv

    from apps.addresses.loaders import AIRLINES_CSV_PATH

    assert Airline.objects.filter(iata="UA", name="United Airlines").exists()
    assert Airline.objects.filter(iata="DL").exists()
    with AIRLINES_CSV_PATH.open(newline="", encoding="utf-8") as fh:
        expected = sum(1 for row in csv.DictReader(fh) if (row["iata"] or "").strip())
    assert Airline.objects.count() == expected


def test_label_is_code_dash_name():
    airline = Airline(iata="UA", name="United Airlines")
    assert airline.label == "UA — United Airlines"
    assert str(airline) == "UA — United Airlines"


def test_airlines_list_alphabetically_by_name():
    names = list(Airline.objects.values_list("name", flat=True))
    # Case-insensitive: MySQL (dev/test) and Postgres (prod) both collate that way.
    assert names == sorted(names, key=str.lower)
