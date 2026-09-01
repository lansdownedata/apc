import pytest
from django.core.management import call_command

from apps.addresses.loaders import load_venues
from apps.addresses.models import Venue

pytestmark = pytest.mark.django_db

HEADER = "name,kind,city,state,vehicle_cap,cap_note,lead_hits\n"
TWO_ROWS = HEADER + "Rose Hill Manor,venue,Leesburg,VA,,,14\nHampton Inn,hotel,Leesburg,VA,,,33\n"


@pytest.fixture(autouse=True)
def _empty_venue_table():
    """Migration 0009 seeds the directory; these tests assert on exact counts."""
    Venue.objects.all().delete()


def _write(tmp_path, body):
    path = tmp_path / "venues.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_rows_and_reports_created(tmp_path):
    created, updated = load_venues(Venue, _write(tmp_path, TWO_ROWS))
    assert (created, updated) == (2, 0)
    assert Venue.objects.count() == 2


def test_reload_updates_in_place_keyed_on_name_and_kind(tmp_path):
    load_venues(Venue, _write(tmp_path, TWO_ROWS))
    rehit = HEADER + "Rose Hill Manor,venue,Leesburg,VA,,,20\nHampton Inn,hotel,Leesburg,VA,,,33\n"
    created, updated = load_venues(Venue, _write(tmp_path, rehit))
    assert (created, updated) == (0, 2)
    assert Venue.objects.get(name="Rose Hill Manor").lead_hits == 20


def test_the_same_name_can_be_both_a_venue_and_a_hotel(tmp_path):
    """Lansdowne Resort is a reception venue AND a room-block hotel — one row each."""
    body = (
        HEADER
        + "Lansdowne Resort,venue,Leesburg,VA,,,11\nLansdowne Resort,hotel,Leesburg,VA,,,11\n"
    )
    load_venues(Venue, _write(tmp_path, body))
    assert Venue.objects.filter(name="Lansdowne Resort").count() == 2


def test_blank_names_are_skipped_and_caps_parsed(tmp_path):
    body = (
        HEADER
        + 'The Oak Barn at Loyalty,venue,Leesburg,VA,40,"Limits shuttles to 40.",10\n,,,,,,\n'
    )
    load_venues(Venue, _write(tmp_path, body))
    venue = Venue.objects.get()
    assert venue.vehicle_cap == 40
    assert venue.cap_note == "Limits shuttles to 40."


def test_the_loader_never_writes_a_street_address(tmp_path):
    """Seeds carry names and towns only — LocationIQ owns the street line."""
    load_venues(Venue, _write(tmp_path, TWO_ROWS))
    assert not Venue.objects.exclude(address="").exists()


def test_seed_venues_command_reports_counts(tmp_path, capsys):
    call_command("seed_venues", path=str(_write(tmp_path, TWO_ROWS)))
    assert "Venues: 2 created, 0 updated." in capsys.readouterr().out


def test_the_committed_csv_has_no_duplicate_name_kind_rows():
    """load_venues keys on (name, kind); a dupe row would silently swallow the other."""
    import csv as _csv

    from apps.addresses.loaders import VENUES_CSV_PATH

    with open(VENUES_CSV_PATH, newline="", encoding="utf-8") as fh:
        keys = [
            ((r["name"] or "").strip(), (r.get("kind") or "venue").strip())
            for r in _csv.DictReader(fh)
            if (r["name"] or "").strip()
        ]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, dupes


def test_the_committed_csv_covers_the_wider_dmv():
    """DC + Maryland + Northern Virginia venues, not just Loudoun/Fauquier."""
    from apps.addresses.loaders import VENUES_CSV_PATH

    load_venues(Venue, VENUES_CSV_PATH)
    for name in (
        "Congressional Country Club",
        "District Winery",
        "Anderson House",
        "Great Marsh Estate",
        "Woodend Sanctuary",
        "Whitehall Estate",  # a pre-existing row, still present
    ):
        assert Venue.objects.filter(name=name).exists(), name
    # New rows carry no fabricated inbound-lead history.
    assert Venue.objects.get(name="District Winery").lead_hits == 0
    assert Venue.objects.filter(state="DC").count() >= 5
    assert Venue.objects.filter(state="MD").count() >= 15
    # The original Loudoun rows are untouched.
    assert Venue.objects.get(name="The Oak Barn at Loyalty").vehicle_cap == 40


def test_the_committed_csv_seeds_the_recurring_places(django_db_blocker):
    """The shipped directory covers the places that recur across the 1,491 inquiries."""
    from apps.addresses.loaders import VENUES_CSV_PATH

    load_venues(Venue, VENUES_CSV_PATH)
    oak = Venue.objects.get(name="The Oak Barn at Loyalty")
    assert oak.vehicle_cap == 40
    assert oak.cap_note
    assert Venue.objects.filter(kind=Venue.Kind.HOTEL, name="Hampton Inn Leesburg").exists()
    assert Venue.objects.filter(kind=Venue.Kind.CHURCH).count() >= 6
