import re
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.dispatch import selectors, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import (
    DriverFactory,
    RenewalFactory,
    VehicleFactory,
    VehicleRenewalFactory,
)
from apps.fleet.models import Driver, Vehicle
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _booked_trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    return ReservationFactory(**kwargs)


# --- model ---


def test_in_house_rows_carry_a_driver_and_no_vendor():
    a = AssignmentFactory(in_house=True)
    assert a.vendor is None and a.driver is not None
    assert a.is_in_house is True
    assert a.status == Assignment.Status.CONFIRMED
    assert a.payout == 0
    assert a.provider_name == a.driver.name
    assert a.driver.name in str(a)


def test_vendor_rows_are_not_in_house():
    a = AssignmentFactory(vendor=VendorFactory(name="Capital Chauffeurs"))
    assert a.is_in_house is False
    assert a.provider_name == "Capital Chauffeurs"


def test_exactly_one_provider_is_enforced_by_the_database():
    trip = ReservationFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(reservation=trip)  # neither
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(reservation=trip, vendor=VendorFactory(), driver=DriverFactory())


def test_a_vehicle_needs_a_driver_at_the_database():
    trip = ReservationFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(
            reservation=trip, vendor=VendorFactory(), vehicle=VehicleFactory()
        )


# --- services ---


def test_assign_in_house_lands_confirmed_with_no_payout():
    trip, driver = _booked_trip(), DriverFactory()
    a = services.assign_in_house(trip, driver, note="his regular")
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at is not None
    assert a.channel == Assignment.Channel.MANUAL
    assert a.payout == Decimal("0")
    assert a.driver == driver and a.vehicle is None and a.vendor is None
    assert a.note == "his regular"
    assert services.active_assignment(trip) == a


def test_assign_in_house_with_a_unit():
    trip, driver, unit = _booked_trip(), DriverFactory(), VehicleFactory()
    a = services.assign_in_house(trip, driver, vehicle=unit)
    assert a.vehicle == unit


def test_in_house_refuses_an_inactive_driver_or_unit():
    trip = _booked_trip()
    with pytest.raises(services.AssignmentError, match="inactive"):
        services.assign_in_house(trip, DriverFactory(status=Driver.Status.INACTIVE))
    with pytest.raises(services.AssignmentError, match="inactive"):
        services.assign_in_house(
            trip, DriverFactory(), vehicle=VehicleFactory(status=Vehicle.Status.INACTIVE)
        )
    assert trip.assignments.count() == 0


def test_claim_refuses_zero_or_two_providers_and_a_vehicle_without_a_driver():
    trip = _booked_trip()
    kwargs = {"note": "", "status": Assignment.Status.CONFIRMED}
    with pytest.raises(services.AssignmentError):
        services._claim(trip, **kwargs)
    with pytest.raises(services.AssignmentError):
        services._claim(trip, vendor=VendorFactory(), driver=DriverFactory(), **kwargs)
    with pytest.raises(services.AssignmentError):
        services._claim(trip, vendor=VendorFactory(), vehicle=VehicleFactory(), **kwargs)


def test_in_house_respects_the_one_active_rule_across_provider_kinds():
    trip = _booked_trip()
    services.send_offer(trip, VendorFactory(), payout=Decimal("100.00"))
    with pytest.raises(services.AssignmentError, match="already"):
        services.assign_in_house(trip, DriverFactory())
    other = _booked_trip()
    services.assign_in_house(other, DriverFactory())
    with pytest.raises(services.AssignmentError, match="already"):
        services.send_offer(other, VendorFactory(), payout=Decimal("100.00"))


def test_in_house_still_needs_a_booked_uncancelled_trip():
    quoted = ReservationFactory(lead=LeadFactory(status=Lead.Status.QUOTED))
    with pytest.raises(services.AssignmentError, match="booked"):
        services.assign_in_house(quoted, DriverFactory())


def test_confirm_and_decline_are_refused_on_in_house_rows():
    a = AssignmentFactory(in_house=True)
    with pytest.raises(services.AssignmentError, match="unassigned"):
        services.confirm(a)
    with pytest.raises(services.AssignmentError, match="unassigned"):
        services.decline(a)
    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED


def test_withdraw_unassigns_an_in_house_row():
    a = AssignmentFactory(in_house=True)
    services.withdraw(a, note="driver sick")
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN
    assert services.active_assignment(a.reservation) is None


def test_release_trips_covers_in_house_rows():
    a = AssignmentFactory(in_house=True)
    released = services.release_trips([a.reservation], note="order cancelled")
    assert [r.pk for r in released] == [a.pk]


def test_existing_vendor_paths_pass_vendor_by_keyword():
    """send_offer / assign_direct must still work exactly as before the signature change."""
    trip = _booked_trip()
    a = services.assign_direct(trip, VendorFactory(), payout=Decimal("140.00"))
    assert a.vendor is not None and a.driver is None and a.is_in_house is False


# --- selectors ---


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("pickup_date", date(2026, 9, 12))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(**kwargs)


def test_in_house_options_is_empty_without_active_drivers():
    DriverFactory(status=Driver.Status.INACTIVE)
    VehicleFactory()
    options = selectors.in_house_options(_trip())
    assert options["drivers"] == []


def test_drivers_rank_most_used_first_then_by_name():
    trip = _trip()
    heavy, light = DriverFactory(name="Zed Heavy"), DriverFactory(name="Al Light")
    for _ in range(2):
        services.withdraw(services.assign_in_house(_trip(), heavy))
    services.withdraw(services.assign_in_house(_trip(), light))
    never = DriverFactory(name="Bo Never")
    ranked = selectors.in_house_options(trip)["drivers"]
    assert [o["driver"].pk for o in ranked] == [heavy.pk, light.pk, never.pk]
    assert ranked[0]["used"] == 2


def test_fitting_units_come_first_and_are_flagged():
    suv = VehicleTypeFactory(name="Luxury SUV")
    trip = _trip(vehicle=suv)
    DriverFactory()
    van = VehicleFactory(name="A Van", vehicle_type=VehicleTypeFactory(name="Sprinter Van"))
    fit = VehicleFactory(name="Z SUV", vehicle_type=suv)
    units = selectors.in_house_options(trip)["vehicles"]
    assert [o["vehicle"].pk for o in units] == [fit.pk, van.pk]
    assert units[0]["fits_vehicle"] is True and units[1]["fits_vehicle"] is False


def test_no_vehicle_class_on_the_trip_means_no_fit_marking():
    trip = _trip(vehicle=None)
    DriverFactory()
    VehicleFactory(name="B"), VehicleFactory(name="A")
    units = selectors.in_house_options(trip)["vehicles"]
    assert [o["vehicle"].name for o in units] == ["A", "B"]
    assert all(o["fits_vehicle"] is False for o in units)


def test_options_carry_the_renewal_state():
    trip = _trip()
    lapsed = DriverFactory(name="Lapsed")
    RenewalFactory(driver=lapsed, expires_on=timezone.localdate() - timedelta(days=3))
    unit = VehicleFactory()
    VehicleRenewalFactory(vehicle=unit, expires_on=timezone.localdate() + timedelta(days=8))
    options = selectors.in_house_options(trip)
    assert options["drivers"][0]["renewal"]["status"] == "expired"
    assert options["vehicles"][0]["renewal"]["status"] == "critical"


def test_in_house_options_query_count_is_flat(django_assert_max_num_queries):
    trip = _trip()
    for _ in range(3):
        RenewalFactory()
        VehicleRenewalFactory()
    with django_assert_max_num_queries(4):
        selectors.in_house_options(trip)
    for _ in range(20):
        RenewalFactory()
        VehicleRenewalFactory()
    with django_assert_max_num_queries(4):
        selectors.in_house_options(trip)


# --- drawer ---


def _panel(client, trip) -> str:
    return client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()


def test_panel_omits_the_in_house_block_without_drivers(logged_in_client):
    body = _panel(logged_in_client, _trip())
    assert ">In-house<" not in body
    assert "dispatch_assign_driver" not in body and "assign-driver" not in body


def test_panel_shows_drivers_and_units_with_a_no_vehicle_default(logged_in_client):
    DriverFactory(name="Marcus Bell")
    VehicleFactory(name="Unit 1")
    trip = _trip()
    body = _panel(logged_in_client, trip)
    assert ">In-house<" in body
    assert "Marcus Bell" in body and "1000" in body
    assert "Unit 1" in body
    assert 'name="vehicle" value="" checked' in body
    assert reverse("dispatch_assign_driver", args=[trip.pk]) in body


def test_panel_warns_on_lapsing_paperwork(logged_in_client):
    d = DriverFactory(name="Marcus Bell")
    RenewalFactory(driver=d, expires_on=timezone.localdate() + timedelta(days=8))
    body = _panel(logged_in_client, _trip())
    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    driver_label = next(label for label in labels if "Marcus Bell" in label)
    assert "Expires in 8 days" in driver_label


def test_panel_shows_an_in_house_coverage_with_unassign_only(logged_in_client):
    trip = _trip()
    a = services.assign_in_house(
        trip, DriverFactory(name="Marcus Bell"), vehicle=VehicleFactory(name="Unit 1")
    )
    body = _panel(logged_in_client, trip)
    assert "Marcus Bell" in body and "Unit 1" in body and "In-house" in body
    assert ">Confirm<" not in body and ">Declined<" not in body
    assert ">Unassign<" in body
    assert "Payout" not in body and "Margin" not in body
    assert reverse("dispatch_resolve", args=[a.pk]) in body


def test_panel_skips_the_in_house_lookup_when_the_trip_is_covered(logged_in_client):
    """A covered trip never renders the In-house block, so it shouldn't pay for it."""
    trip = _trip()
    DriverFactory()
    VehicleFactory()
    services.assign_in_house(trip, DriverFactory())
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.context["in_house"] == {"drivers": [], "vehicles": []}


def test_panel_still_shows_payout_for_a_vendor_coverage(logged_in_client):
    trip = _trip()
    services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
    body = _panel(logged_in_client, trip)
    assert "Payout" in body and "Margin" in body and ">Withdraw<" in body


def test_panel_without_drivers_keeps_its_query_budget(
    logged_in_client, django_assert_max_num_queries
):
    trip = _trip(stops=[f"Stop {i}" for i in range(8)])
    # Budget 10, not 9: selectors.in_house_options adds one query (the active-drivers read)
    # even when there are no drivers to show.
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))


# --- views ---


def test_assign_driver_view_confirms_with_a_unit(logged_in_client):
    trip, driver, unit = _trip(), DriverFactory(), VehicleFactory()
    resp = logged_in_client.post(
        reverse("dispatch_assign_driver", args=[trip.pk]),
        {"driver": driver.pk, "vehicle": unit.pk, "note": "regular"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    a = services.active_assignment(trip)
    assert a.driver == driver and a.vehicle == unit and a.status == Assignment.Status.CONFIRMED


def test_assign_driver_view_accepts_no_vehicle(logged_in_client):
    trip, driver = _trip(), DriverFactory()
    logged_in_client.post(
        reverse("dispatch_assign_driver", args=[trip.pk]), {"driver": driver.pk, "vehicle": ""}
    )
    assert services.active_assignment(trip).vehicle is None


def test_assign_driver_view_rejects_a_missing_or_inactive_driver(logged_in_client):
    trip = _trip()
    resp = logged_in_client.post(reverse("dispatch_assign_driver", args=[trip.pk]), {"vehicle": ""})
    assert resp.status_code == 400 and "driver" in resp.json()["error"].lower()
    gone = DriverFactory(status=Driver.Status.INACTIVE)
    resp = logged_in_client.post(
        reverse("dispatch_assign_driver", args=[trip.pk]), {"driver": gone.pk}
    )
    assert resp.status_code == 400


def test_assign_driver_view_rejects_get(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_assign_driver", args=[_trip().pk]))
    assert resp.status_code == 405


def test_assign_driver_view_requires_login(client):
    resp = client.post(reverse("dispatch_assign_driver", args=[_trip().pk]), {"driver": 1})
    assert resp.status_code == 302


def test_resolve_refuses_confirm_and_decline_on_in_house(logged_in_client):
    a = AssignmentFactory(in_house=True)
    for action in ("confirm", "decline"):
        resp = logged_in_client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": action})
        assert resp.status_code == 400
    resp = logged_in_client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": "withdraw"})
    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN


# --- board ---


def test_board_names_the_driver_with_a_house_icon(logged_in_client):
    day = timezone.localdate() + timedelta(days=30)
    trip = _trip(pickup_date=day)
    services.assign_in_house(trip, DriverFactory(name="Marcus Bell"))
    body = logged_in_client.get(
        reverse("dispatch_board"), {"day": day.isoformat()}
    ).content.decode()
    assert "Marcus Bell" in body
    assert "ti-home" in body
    assert b">Covered by<" in body.encode()
