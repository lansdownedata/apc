"""A transfer's drop-off = pickup + max(billed minimum, actual drive time).

Transfers used to carry no end time at all, which left dispatch and the affiliate trip
sheet with an open-ended job. The minimum alone is not enough either — a two-hour minimum
on a three-hour drive would promise an end the vehicle cannot make.

An agent's own drop-off always wins: this only ever fills a blank.
"""

from datetime import date, time
from decimal import Decimal

import pytest

from apps.reservations.services import derive_dropoff

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_drive(monkeypatch):
    """No coordinates / no key — the fallback path."""
    monkeypatch.setattr("apps.integrations.geocoding.drive_seconds", lambda *a: None)


@pytest.fixture
def drive(monkeypatch):
    def _set(seconds):
        monkeypatch.setattr("apps.integrations.geocoding.drive_seconds", lambda *a: seconds)

    return _set


def _pickup():
    return date(2027, 9, 4), time(16, 0)


def test_the_minimum_applies_when_the_drive_is_shorter(no_drive):
    end = derive_dropoff(*_pickup(), billed_hours=Decimal("2"), drive_seconds=45 * 60)
    assert end == (date(2027, 9, 4), time(18, 0))


def test_the_drive_wins_when_it_is_longer_than_the_minimum():
    """A three-hour drive on a two-hour minimum ends when the vehicle actually arrives."""
    end = derive_dropoff(*_pickup(), billed_hours=Decimal("2"), drive_seconds=3 * 60 * 60)
    assert end == (date(2027, 9, 4), time(19, 0))


def test_it_rolls_the_date_past_midnight():
    """A late run ends tomorrow — the date has to move with it (CLAUDE.md: the trip's own
    timezone decides the date, so this must never silently stay on the pickup day)."""
    end = derive_dropoff(date(2027, 9, 4), time(23, 0), billed_hours=Decimal("2"), drive_seconds=0)
    assert end == (date(2027, 9, 5), time(1, 0))


def test_no_drive_time_available_falls_back_to_the_minimum():
    end = derive_dropoff(*_pickup(), billed_hours=Decimal("2"), drive_seconds=None)
    assert end == (date(2027, 9, 4), time(18, 0))


def test_no_minimum_and_no_drive_time_yields_nothing():
    """Rather than an end equal to the pickup, which reads as a zero-length trip."""
    assert derive_dropoff(*_pickup(), billed_hours=Decimal("0"), drive_seconds=None) is None


def test_a_drive_with_no_minimum_still_produces_an_end():
    end = derive_dropoff(*_pickup(), billed_hours=Decimal("0"), drive_seconds=30 * 60)
    assert end == (date(2027, 9, 4), time(16, 30))


def test_a_missing_pickup_yields_nothing():
    assert derive_dropoff(None, time(16, 0), billed_hours=Decimal("2"), drive_seconds=None) is None
    assert (
        derive_dropoff(date(2027, 9, 4), None, billed_hours=Decimal("2"), drive_seconds=0) is None
    )
