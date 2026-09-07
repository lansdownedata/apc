"""Every wedding trip arrives priced-in, not blank.

The public flow used to assign no vehicle at all, on the reasoning that snapshotting a
rate card off a customer's guess was worse than leaving it unset. In practice that left
the office a ten-trip quote reading $0.00 with a vehicle to pick by hand on every row,
and nothing filled it in later. The vehicle is a *starting point* an agent can change —
the quote is not sent until they send it.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from apps.leads.models import VehicleType
from apps.public.forms import WeddingRequestForm
from apps.public.services import create_lead_from_wedding, send_wedding_confirmation

from .test_wedding_form import _post

pytestmark = pytest.mark.django_db


def _lead(**over):
    form = WeddingRequestForm(_post(**over))
    assert form.is_valid(), form.errors
    return create_lead_from_wedding(form.cleaned_data)


def test_every_generated_trip_gets_a_vehicle():
    for res in _lead().reservations.all():
        assert res.vehicle is not None, res.source_leg_id


def test_the_vehicle_seats_that_trips_own_share_of_the_group():
    """Each trip carries its share of a split run, so the vehicle is sized to the share —
    not to the whole movement, which would send two 56-seaters for 60 guests."""
    for res in _lead().reservations.all():
        assert res.vehicle.capacity >= res.passengers


def test_the_smallest_vehicle_that_fits_is_the_one_chosen():
    """A two-person exit run must not arrive holding a coach."""
    couple = _lead().reservations.order_by("passengers").first()
    smallest = (
        VehicleType.objects.filter(
            active=True, group_transport=True, capacity__gte=couple.passengers
        )
        .order_by("capacity", "sort_order")
        .first()
    )
    assert couple.vehicle == smallest


def test_the_rate_card_is_snapshotted_off_the_chosen_vehicle():
    """Faithfulness to the catalog, not "> 0": a vehicle with no rate set in Settings
    correctly snapshots 0, and the fix for that is entering its rate, not guessing one.
    """
    for res in _lead().reservations.all():
        assert res.rate == res.vehicle.rate
        assert res.min_hours == res.vehicle.transfer_min_hours


def test_a_priced_catalog_produces_a_non_zero_quote():
    """The regression behind the $0.00 workspace — no vehicle meant no rate, always."""
    VehicleType.objects.filter(rate=0).update(rate=Decimal("150.00"))
    assert _lead().quote_total > 0


def test_a_limo_never_lands_on_a_wedding_trip():
    """Capacity alone would pick one for a small family leg."""
    for res in _lead().reservations.all():
        assert res.vehicle.group_transport is True


def test_prefilling_does_not_cost_a_query_per_trip():
    """The catalog is read once, not once per generated vehicle."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    form = WeddingRequestForm(_post())
    assert form.is_valid(), form.errors
    with CaptureQueriesContext(connection) as ctx:
        create_lead_from_wedding(form.cleaned_data)
    fleet_reads = [q for q in ctx.captured_queries if "group_transport" in q["sql"]]
    assert len(fleet_reads) <= 1, fleet_reads


# --- the derived trip window -----------------------------------------------------------


def test_every_trip_gets_an_end_time():
    """A transfer used to reach dispatch and the affiliate sheet open-ended."""
    for res in _lead().reservations.all():
        assert res.dropoff_time is not None
        assert res.dropoff_estimated is True


def test_the_end_is_the_pickup_plus_the_vehicles_billed_minimum(monkeypatch):
    monkeypatch.setattr("apps.integrations.geocoding.drive_seconds", lambda *a: None)
    res = _lead().reservations.order_by("sort_order").first()
    expected = datetime.combine(res.pickup_date, res.pickup_time) + timedelta(
        hours=float(res.billed_hours)
    )
    assert (res.dropoff_date, res.dropoff_time) == (expected.date(), expected.time())


def test_a_long_drive_pushes_the_end_past_the_minimum(monkeypatch):
    """The minimum is what we bill; the drive is how long the vehicle is actually gone."""
    monkeypatch.setattr("apps.integrations.geocoding.drive_seconds", lambda *a: 5 * 60 * 60)
    res = _lead().reservations.order_by("sort_order").first()
    hours = (
        datetime.combine(res.dropoff_date, res.dropoff_time)
        - datetime.combine(res.pickup_date, res.pickup_time)
    ).total_seconds() / 3600
    assert hours == 5


def test_a_late_run_rolls_the_end_onto_the_next_date():
    """CLAUDE.md: a date boundary follows the trip, so an 11pm run ends tomorrow."""
    late = [r for r in _lead().reservations.all() if r.pickup_time.hour >= 23]
    assert late, "fixture no longer has a late leg"
    for res in late:
        assert res.dropoff_date > res.pickup_date


# --- the confirmation email -------------------------------------------------------------


def _email(settings, mailoutbox):
    settings.PUBLIC_BASE_URL = "https://allprocharter.com"
    lead = _lead()
    send_wedding_confirmation(lead, base_url=settings.PUBLIC_BASE_URL)
    assert mailoutbox, "no confirmation email was sent"
    return lead, mailoutbox[-1]


def test_the_email_lists_movements_not_one_line_per_coach(settings, mailoutbox):
    """Same rule as the thanks page (APC-14): the couple asked for 105 guests on one
    movement and must read 105 back, once — not two lines of 53 and 52."""
    _, msg = _email(settings, mailoutbox)
    body = " ".join(msg.body.split())
    assert "105 passengers" in body
    assert "53 passengers" not in body
    assert body.count("Hampton Inn Leesburg ->") == 1


def test_the_email_names_the_vehicle(settings, mailoutbox):
    """Now that a vehicle is assigned up front, the couple can read what is turning up."""
    _, msg = _email(settings, mailoutbox)
    assert "Motor Coach" in msg.body


def test_the_email_counts_movements_not_trips(settings, mailoutbox):
    _, msg = _email(settings, mailoutbox)
    assert "all 2 movements" in " ".join(msg.body.split())
