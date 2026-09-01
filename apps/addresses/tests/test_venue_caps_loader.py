"""`load_venue_caps` — the bulk path for the client's venue -> vehicle-limit list.

Unlike `load_venues` this never creates a row and never touches any field other than
`vehicle_cap` / `cap_note`, so the client's (partial) list can be re-run against the
directory without disturbing anything else (APC-9).
"""

import pytest
from django.core.management import call_command

from apps.addresses.factories import VenueFactory
from apps.addresses.loaders import load_venue_caps
from apps.addresses.models import Venue

pytestmark = pytest.mark.django_db

HEADER = "name,kind,vehicle_cap,cap_note\n"


@pytest.fixture(autouse=True)
def _empty_venue_table():
    """Migration 0009 seeds the directory; these tests assert on exact counts."""
    Venue.objects.all().delete()


def _write(tmp_path, body):
    path = tmp_path / "venue_caps.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_sets_cap_and_note_on_an_existing_venue(tmp_path):
    VenueFactory(name="Congressional Country Club", kind=Venue.Kind.VENUE, city="Bethesda")
    body = HEADER + 'Congressional Country Club,venue,34,"Gatehouse limits shuttles to 34."\n'

    updated, skipped = load_venue_caps(Venue, _write(tmp_path, body))

    assert (updated, skipped) == (1, [])
    venue = Venue.objects.get(name="Congressional Country Club")
    assert venue.vehicle_cap == 34
    assert venue.cap_note == "Gatehouse limits shuttles to 34."


def test_never_creates_a_row(tmp_path):
    body = HEADER + "Some Venue Not In The Directory,venue,20,narrow drive\n"

    updated, skipped = load_venue_caps(Venue, _write(tmp_path, body))

    assert updated == 0
    assert skipped == [("Some Venue Not In The Directory", "venue")]
    assert not Venue.objects.exists()


def test_leaves_every_other_field_untouched(tmp_path):
    VenueFactory(
        name="Rose Hill Manor",
        kind=Venue.Kind.VENUE,
        city="Leesburg",
        state="VA",
        lead_hits=14,
        is_active=True,
    )
    body = HEADER + "Rose Hill Manor,venue,45,gravel turning circle\n"

    load_venue_caps(Venue, _write(tmp_path, body))

    venue = Venue.objects.get(name="Rose Hill Manor")
    assert (venue.city, venue.state, venue.lead_hits, venue.is_active) == (
        "Leesburg",
        "VA",
        14,
        True,
    )
    assert venue.vehicle_cap == 45


def test_keyed_on_name_and_kind(tmp_path):
    VenueFactory(name="Lansdowne Resort", kind=Venue.Kind.VENUE)
    VenueFactory(name="Lansdowne Resort", kind=Venue.Kind.HOTEL)
    body = HEADER + "Lansdowne Resort,venue,50,\n"

    load_venue_caps(Venue, _write(tmp_path, body))

    assert Venue.objects.get(name="Lansdowne Resort", kind=Venue.Kind.VENUE).vehicle_cap == 50
    assert Venue.objects.get(name="Lansdowne Resort", kind=Venue.Kind.HOTEL).vehicle_cap is None


def test_kind_defaults_to_venue_when_column_is_blank(tmp_path):
    VenueFactory(name="Stone Tower Winery", kind=Venue.Kind.VENUE)
    body = "name,kind,vehicle_cap,cap_note\nStone Tower Winery,,38,\n"

    updated, _ = load_venue_caps(Venue, _write(tmp_path, body))

    assert updated == 1
    assert Venue.objects.get(name="Stone Tower Winery").vehicle_cap == 38


def test_a_blank_cap_cell_clears_an_existing_cap(tmp_path):
    VenueFactory(
        name="Rust Manor House", kind=Venue.Kind.VENUE, vehicle_cap=30, cap_note="old note"
    )
    body = HEADER + "Rust Manor House,venue,,\n"

    load_venue_caps(Venue, _write(tmp_path, body))

    venue = Venue.objects.get(name="Rust Manor House")
    assert venue.vehicle_cap is None
    assert venue.cap_note == ""


def test_blank_name_rows_are_skipped(tmp_path):
    VenueFactory(name="Raspberry Plain Manor", kind=Venue.Kind.VENUE)
    body = HEADER + "Raspberry Plain Manor,venue,40,\n,,,\n"

    updated, skipped = load_venue_caps(Venue, _write(tmp_path, body))

    assert updated == 1
    assert skipped == []


def test_command_caps_mode_reports_counts_and_unmatched(tmp_path, capsys):
    VenueFactory(name="Rose Hill Manor", kind=Venue.Kind.VENUE)
    body = HEADER + "Rose Hill Manor,venue,45,\nGhost Venue,venue,10,\n"

    call_command("seed_venues", caps=True, path=str(_write(tmp_path, body)))

    out = capsys.readouterr().out
    assert "Venue caps: 1 updated." in out
    assert "Ghost Venue" in out


def test_command_caps_mode_requires_a_path():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("seed_venues", caps=True)
