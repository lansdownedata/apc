"""APC-72 — warn, don't block (D4): the coverage controls flag an affiliate whose
insurance has lapsed, or will before the trip, and offering or confirming to one records
who overrode the warning.

Measured on the trip's own service date and "today" in the trip's zone, never the
server's."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import selectors, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.dispatch.views import coverage_context
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory, VendorInsuranceFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db

TRIP_DAY = date(2026, 10, 20)
TODAY = date(2026, 10, 1)


def _vendor(*expiries, effective=date(2025, 1, 1), **fields):
    vendor = VendorFactory(**fields)
    for expiry in expiries:
        VendorInsuranceFactory(vendor=vendor, effective_date=effective, expiry_date=expiry)
    return Vendor.objects.prefetch_related("policies").get(pk=vendor.pk)


# --- the rule, on the boundary dates -------------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "status"),
    [
        (TRIP_DAY - timedelta(days=1), "expires_before_trip"),  # the day before the trip
        (TRIP_DAY, "covered"),  # the day of: still covered that day
        (TRIP_DAY + timedelta(days=1), "covered"),
        (TODAY - timedelta(days=1), "expired"),  # lapsed already
        (TODAY, "expires_before_trip"),  # good today, gone by the trip
    ],
)
def test_coverage_on_the_boundary_dates(expiry, status):
    assert _vendor(expiry).coverage_on(TRIP_DAY, today=TODAY) == status


def test_no_policy_on_file():
    assert _vendor().coverage_on(TRIP_DAY, today=TODAY) == "none"


def test_the_best_policy_wins():
    """A renewal on file covers the trip even while the old policy lapses first."""
    vendor = _vendor(TODAY - timedelta(days=3), TRIP_DAY + timedelta(days=200))

    assert vendor.coverage_on(TRIP_DAY, today=TODAY) == "covered"


def test_a_policy_that_only_starts_after_the_trip_does_not_cover_it():
    vendor = _vendor(TRIP_DAY + timedelta(days=365), effective=TRIP_DAY + timedelta(days=1))

    assert vendor.coverage_on(TRIP_DAY, today=TODAY) != "covered"


def test_today_is_the_trips_today_not_the_servers():
    """08:00 UTC on Oct 2 is still Oct 1 in Honolulu. A policy that ran out on Oct 1 has
    lapsed in New York, but in the trip's zone it's good through today."""
    trip = ReservationFactory(
        pickup_date=TRIP_DAY, pickup_time=time(9, 0), pickup_timezone="Pacific/Honolulu"
    )
    vendor = _vendor(date(2026, 10, 1))
    now = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)

    assert selectors.trip_today(trip, now=now) == date(2026, 10, 1)
    assert selectors.insurance_for(vendor, trip, now=now)["status"] == "expires_before_trip"


# --- the badge in the picker ---------------------------------------------------------


def _booked_trip(**extra):
    today = timezone.localdate()
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=today + timedelta(days=20),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
        **extra,
    )


def test_the_badge_rides_on_each_affiliate_option():
    trip = _booked_trip()
    today = timezone.localdate()
    _vendor(today - timedelta(days=2), name="Lapsed Limo")
    _vendor(today + timedelta(days=5), name="Soon Sedans")
    _vendor(name="Bare Buses")
    _vendor(today + timedelta(days=400), name="Fine Fleet")

    options = {o["label"]: o for o in coverage_context(trip)["vendor_options"]}

    assert options["Lapsed Limo"]["alert"] == "Insurance expired"
    assert options["Soon Sedans"]["alert"] == "Expires before trip"
    assert options["Bare Buses"]["alert"] == "No insurance on file"
    assert options["Fine Fleet"]["alert"] == ""


def test_the_badge_renders_in_the_drawer(client):
    client.force_login(UserFactory())
    trip = _booked_trip()
    _vendor(timezone.localdate() - timedelta(days=2), name="Lapsed Limo")

    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    assert "Insurance expired" in body


def test_the_pickers_query_count_does_not_scale_per_vendor():
    trip = _booked_trip()
    today = timezone.localdate()
    _vendor(today + timedelta(days=400))

    with CaptureQueriesContext(connection) as one:
        selectors.vendor_rich_options(selectors.vendor_options(trip, limit=None))
    for n in range(6):
        _vendor(today - timedelta(days=n + 1), today + timedelta(days=n))
    with CaptureQueriesContext(connection) as seven:
        selectors.vendor_rich_options(selectors.vendor_options(trip, limit=None))

    assert len(seven) == len(one)


# --- offering or confirming anyway records the override ------------------------------


def test_offering_to_a_lapsed_affiliate_succeeds_and_records_the_override(client):
    user = UserFactory()
    client.force_login(user)
    trip = _booked_trip()
    vendor = _vendor(timezone.localdate() - timedelta(days=2))

    resp = client.post(
        reverse("dispatch_offer", args=[trip.pk]), {"vendor": vendor.pk, "payout": "300"}
    )

    assert resp.json()["ok"] is True
    a = services.active_assignment(trip)
    assert a.status == Assignment.Status.OFFERED
    assert a.insurance_override == "expired"
    assert a.insurance_override_by == user
    assert a.insurance_override_at is not None


def test_marking_assigned_records_the_override_too(client):
    client.force_login(UserFactory())
    trip = _booked_trip()
    vendor = _vendor()

    client.post(reverse("dispatch_assign", args=[trip.pk]), {"vendor": vendor.pk, "payout": "1"})

    assert services.active_assignment(trip).insurance_override == "none"


def test_confirming_an_offer_records_the_override(client):
    user = UserFactory()
    client.force_login(user)
    trip = _booked_trip()
    vendor = _vendor(trip.pickup_date - timedelta(days=1))
    a = AssignmentFactory(reservation=trip, vendor=vendor, status=Assignment.Status.OFFERED)

    client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": "confirm"})

    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED
    assert (a.insurance_override, a.insurance_override_by) == ("expires_before_trip", user)


def test_a_covered_affiliate_records_no_override(client):
    client.force_login(UserFactory())
    trip = _booked_trip()
    vendor = _vendor(trip.pickup_date + timedelta(days=30))

    client.post(reverse("dispatch_offer", args=[trip.pk]), {"vendor": vendor.pk, "payout": "1"})

    a = services.active_assignment(trip)
    assert a.insurance_override == "" and a.insurance_override_by is None


def test_the_confirm_button_carries_the_warning_and_the_card_shows_the_override(client):
    user = UserFactory(first_name="Dana", last_name="Reyes")
    client.force_login(user)
    trip = _booked_trip()
    vendor = _vendor(timezone.localdate() - timedelta(days=2))
    client.post(reverse("dispatch_offer", args=[trip.pk]), {"vendor": vendor.pk, "payout": "1"})

    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    assert "Insurance expired" in body  # the confirm warning's copy
    assert "Insurance warning overridden by Dana Reyes" in body


def test_the_controls_confirm_through_the_modal_never_natively():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index("function coverageControls")
    body = js[start : js.index("window.coverageControls", start)]
    assert "insuranceCheck" in body
    assert 'variant: "danger"' in body
    assert "window.confirm" not in body and "confirm(" not in body.replace(".confirm(", "")
