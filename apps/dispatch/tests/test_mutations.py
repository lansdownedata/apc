from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.dispatch import services, views
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(**kwargs)


def test_offer_creates_an_offered_assignment(logged_in_client):
    trip, vendor = _trip(), VendorFactory()
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[trip.pk]), {"vendor": vendor.pk, "payout": "215.00"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    a = services.active_assignment(trip)
    assert a.vendor == vendor and a.status == Assignment.Status.OFFERED
    assert a.payout == Decimal("215.00")


def test_direct_assign_confirms_immediately(logged_in_client):
    trip, vendor = _trip(), VendorFactory()
    logged_in_client.post(
        reverse("dispatch_assign", args=[trip.pk]),
        {"vendor": vendor.pk, "payout": "215.00", "note": "by phone"},
    )
    a = services.active_assignment(trip)
    assert a.status == Assignment.Status.CONFIRMED
    assert a.note == "by phone"


def test_double_assign_is_refused_with_a_message(logged_in_client):
    trip = _trip()
    services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
    resp = logged_in_client.post(
        reverse("dispatch_assign", args=[trip.pk]),
        {"vendor": VendorFactory().pk, "payout": "120.00"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert "already" in resp.json()["error"].lower()


def test_missing_vendor_is_rejected(logged_in_client):
    resp = logged_in_client.post(reverse("dispatch_offer", args=[_trip().pk]), {"payout": "215.00"})
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_bad_payout_is_rejected(logged_in_client):
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[_trip().pk]),
        {"vendor": VendorFactory().pk, "payout": "not-money"},
    )
    assert resp.status_code == 400


def test_negative_payout_is_rejected(logged_in_client):
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[_trip().pk]),
        {"vendor": VendorFactory().pk, "payout": "-5.00"},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("payout", ["NaN", "Infinity"])
def test_non_finite_payout_is_rejected(logged_in_client, payout):
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[_trip().pk]),
        {"vendor": VendorFactory().pk, "payout": payout},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.parametrize("endpoint", ["dispatch_offer", "dispatch_assign"])
def test_a_trip_on_an_unsold_quote_is_refused(logged_in_client, endpoint):
    """A hand-crafted POST must not farm out a quote nobody bought."""
    trip = _trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    resp = logged_in_client.post(
        reverse(endpoint, args=[trip.pk]), {"vendor": VendorFactory().pk, "payout": "215.00"}
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert not Assignment.objects.filter(reservation=trip).exists()


@pytest.mark.parametrize("endpoint", ["dispatch_offer", "dispatch_assign"])
def test_a_cancelled_trip_is_refused(logged_in_client, endpoint):
    trip = _trip(trip_status=Reservation.TripStatus.CANCELLED)
    resp = logged_in_client.post(
        reverse(endpoint, args=[trip.pk]), {"vendor": VendorFactory().pk, "payout": "215.00"}
    )
    assert resp.status_code == 400
    assert not Assignment.objects.filter(reservation=trip).exists()


def test_an_inactive_affiliate_is_refused(logged_in_client):
    """The picker only offers active affiliates; the endpoint must agree."""
    trip = _trip()
    archived = VendorFactory(status=Vendor.Status.INACTIVE)
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[trip.pk]), {"vendor": archived.pk, "payout": "215.00"}
    )
    assert resp.status_code == 400
    assert not Assignment.objects.filter(reservation=trip).exists()


@pytest.mark.parametrize("posted,expected", [("100.999", "101.00"), ("100.005", "100.01")])
def test_payout_is_quantized_to_cents_before_it_reaches_the_db(rf, posted, expected):
    """MySQL rounds a third decimal half-even and Postgres half-up, so dev and prod would
    store different money. Round in Python instead, half-up like the rest of the app."""
    assert views._payout(rf.post("/", {"payout": posted})) == Decimal(expected)


@pytest.mark.parametrize(
    "action,expected",
    [
        ("confirm", Assignment.Status.CONFIRMED),
        ("decline", Assignment.Status.DECLINED),
        ("withdraw", Assignment.Status.WITHDRAWN),
    ],
)
def test_resolve_actions(logged_in_client, action, expected):
    a = services.send_offer(_trip(), VendorFactory(), payout=Decimal("100.00"))
    resp = logged_in_client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": action})
    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.status == expected


def test_unknown_resolve_action_is_rejected(logged_in_client):
    a = services.send_offer(_trip(), VendorFactory(), payout=Decimal("100.00"))
    resp = logged_in_client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": "explode"})
    assert resp.status_code == 400


def test_mutations_reject_get(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_offer", args=[_trip().pk]))
    assert resp.status_code == 405


def test_mutations_require_login(client):
    resp = client.post(reverse("dispatch_offer", args=[_trip().pk]), {})
    assert resp.status_code == 302
