from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.dispatch import services, views
from apps.dispatch.factories import AssignmentFactory
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


@pytest.mark.parametrize("payout", ["1e25", "1e26", "1e30", "99999999.999", "100000000"])
def test_an_oversized_payout_is_refused_cleanly(logged_in_client, payout):
    """Every bad payout has to come back as a 400, not a 500.

    `99999999.999` is under the column ceiling until it is rounded and 1e8 after — it must be
    refused too, or it reaches MoneyField(max_digits=10) and dies in the database. And the
    magnitude has to be judged before rounding: `Decimal.quantize` raises InvalidOperation
    once the result would exceed the 28-digit context precision, and that escapes the view's
    `except AssignmentError` as a 500.
    """
    trip = _trip()
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[trip.pk]), {"vendor": VendorFactory().pk, "payout": payout}
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert not Assignment.objects.filter(reservation=trip).exists()


def test_a_payout_just_under_the_ceiling_is_still_stored_rounded(logged_in_client):
    """The companion to the guard above: rounding still happens for values in range."""
    trip = _trip()
    resp = logged_in_client.post(
        reverse("dispatch_offer", args=[trip.pk]),
        {"vendor": VendorFactory().pk, "payout": "215.005"},
    )
    assert resp.status_code == 200
    assert services.active_assignment(trip).payout == Decimal("215.01")


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


# --- a GNet assignment resolves from the affiliate's callback, never a staff click ---


def _gnet_offer(**kwargs) -> Assignment:
    """An offered GNet assignment already live on the gateway."""
    kwargs.setdefault("reservation", _trip())
    kwargs.setdefault("vendor", VendorFactory(gnet_grid_id="gnet-partner-1"))
    kwargs.setdefault("channel", Assignment.Channel.GNET)
    kwargs.setdefault("gnet_transaction_id", "TX-LIVE")
    kwargs.setdefault("status", Assignment.Status.OFFERED)
    kwargs.setdefault("payout", Decimal("140.00"))
    return AssignmentFactory(**kwargs)


@pytest.mark.parametrize("action", ["confirm", "decline"])
def test_staff_marking_a_gnet_assignment_is_refused(logged_in_client, action):
    """`decline` never releases the gateway and `_resolve` then refuses a resolved
    assignment, so one click used to strand a REAL booking with no path in the portal
    that could ever cancel it — while the board showed the trip uncovered, inviting a
    re-offer that books a SECOND vehicle. Both staff marks are refused on this channel.
    """
    a = _gnet_offer()
    with patch.object(services, "gnet_sync") as mock_sync:
        resp = logged_in_client.post(reverse("dispatch_resolve", args=[a.pk]), {"action": action})

    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert "withdraw" in body["error"].lower()
    a.refresh_from_db()
    assert a.status == Assignment.Status.OFFERED
    assert a.gnet_transaction_id == "TX-LIVE"
    assert mock_sync.mock_calls == []


def test_withdraw_still_resolves_a_gnet_assignment_and_releases_the_gateway(logged_in_client):
    """Withdraw stays the one staff action on this channel precisely because it is the
    only one that cancels the trip on the gateway."""
    a = _gnet_offer()
    with patch.object(services, "gnet_sync") as mock_sync:
        resp = logged_in_client.post(
            reverse("dispatch_resolve", args=[a.pk]), {"action": "withdraw"}
        )

    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN
    assert mock_sync.cancel_assignment.call_count == 1


def test_mutations_reject_get(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_offer", args=[_trip().pk]))
    assert resp.status_code == 405


def test_mutations_require_login(client):
    resp = client.post(reverse("dispatch_offer", args=[_trip().pk]), {})
    assert resp.status_code == 302
