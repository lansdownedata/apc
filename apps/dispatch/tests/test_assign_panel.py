import re
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.dispatch import selectors, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory, VendorInsuranceFactory

pytestmark = pytest.mark.django_db


def _trip(**kwargs):
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(lead=lead, **kwargs)


def test_vendors_are_ranked_by_how_often_they_are_used():
    trip = _trip()
    heavy, light, unused = VendorFactory(), VendorFactory(), VendorFactory()
    for _ in range(3):
        services.decline(services.send_offer(_trip(), heavy, payout=Decimal("10.00")))
    services.decline(services.send_offer(_trip(), light, payout=Decimal("10.00")))

    ranked = selectors.vendor_options(trip)
    assert [o["vendor"].pk for o in ranked[:2]] == [heavy.pk, light.pk]
    assert ranked[0]["used"] == 3
    assert unused.pk in [o["vendor"].pk for o in ranked]


def test_ranking_is_capped_at_the_limit():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    assert len(selectors.vendor_options(trip, limit=8)) == 8


def test_vendor_options_query_count_is_flat_regardless_of_vendor_count(
    django_assert_max_num_queries,
):
    """Guards against reintroducing an N+1, e.g. a per-vendor insurance lookup.

    The bound must hold at two very different vendor counts to prove the query count is
    flat, not just under some ceiling that a mild N+1 would still clear.
    """
    trip = _trip()
    for _ in range(3):
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)

    for _ in range(27):  # 30 active vendors total
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)


def test_search_looks_past_the_top_slice():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    needle = VendorFactory(name="Zebra Executive")
    assert [o["vendor"].pk for o in selectors.vendor_options(trip, search="zebra")] == [needle.pk]


def test_options_carry_vehicle_fit():
    suv = VehicleTypeFactory(name="SUV")
    trip = _trip(vehicle=suv)
    fitting = VendorFactory()
    fitting.vehicle_types.add(suv)
    VendorFactory()
    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[fitting.pk]["fits_vehicle"] is True
    assert all(o["fits_vehicle"] is False for pk, o in by_pk.items() if pk != fitting.pk)


def test_options_carry_the_insurance_state():
    """Lapsed coverage is the one fit signal with legal consequences for a broker, and the
    drawer is where the assignment decision actually gets made."""
    trip = _trip()
    lapsed = VendorFactory(name="Lapsed Ltd")
    VendorInsuranceFactory(vendor=lapsed, expiry_date=timezone.localdate() - timedelta(days=3))
    insured = VendorFactory(name="Insured Co")
    VendorInsuranceFactory(vendor=insured, expiry_date=timezone.localdate() + timedelta(days=200))
    bare = VendorFactory(name="Bare Inc")

    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[lapsed.pk]["insurance"]["status"] == "expired"
    assert by_pk[insured.pk]["insurance"]["status"] == "valid"
    assert by_pk[bare.pk]["insurance"]["status"] == "none"


def test_panel_shows_the_insurance_state(logged_in_client):
    trip = _trip()
    lapsed = VendorFactory(name="Lapsed Ltd")
    VendorInsuranceFactory(vendor=lapsed, expiry_date=timezone.localdate() - timedelta(days=3))
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content
    assert b"Lapsed 3 days ago" in body


def test_panel_renders_money_as_money(logged_in_client):
    trip = _trip(rate=Decimal("1200.00"), hours=1)
    services.assign_direct(trip, VendorFactory(), payout=Decimal("1000.00"))
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content
    assert b"$1,200.00" in body  # customer total
    assert b"$1,000.00" in body  # payout
    assert b"$200.00" in body  # margin


def test_panel_renders_the_trip_and_vendors(logged_in_client):
    trip = _trip()
    VendorFactory(name="Capital Chauffeurs")
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 200
    assert b"Capital Chauffeurs" in resp.content
    assert resp.context["trip"].pk == trip.pk
    assert resp.context["assignment"] is None


def test_panel_shows_the_active_assignment_when_there_is_one(logged_in_client):
    trip = _trip()
    services.assign_direct(trip, VendorFactory(name="Chesapeake"), payout=Decimal("200.00"))
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.context["assignment"].vendor.name == "Chesapeake"
    assert b"Chesapeake" in resp.content


def test_panel_requires_login(client):
    trip = _trip()
    resp = client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 302


# --- GNet channel hint ---


def test_options_carry_the_gnet_flag():
    trip = _trip()
    grid = VendorFactory(name="Grid Co", gnet_grid_id="gnet-1")
    manual = VendorFactory(name="Manual Co")
    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[grid.pk]["is_gnet"] is True
    assert by_pk[manual.pk]["is_gnet"] is False


def test_panel_shows_the_gnet_badge_for_a_gnet_capable_vendor_only(logged_in_client):
    trip = _trip()
    VendorFactory(name="Grid Co", gnet_grid_id="gnet-1")
    VendorFactory(name="Manual Co")
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    grid_label = next(label for label in labels if "Grid Co" in label)
    manual_label = next(label for label in labels if "Manual Co" in label)
    assert "GNET" in grid_label
    assert "GNET" not in manual_label


# --- staff-marking buttons are for non-GNet vendors only ---


def _panel(logged_in_client, trip) -> str:
    return logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()


def test_panel_hides_confirm_and_declined_for_a_gnet_offer(logged_in_client):
    """A GNet offer resolves from the affiliate's callback. Staff-marking it declined
    would strand a real booking (see test_mutations), so the buttons aren't rendered —
    with a line of copy so their absence isn't a mystery."""
    trip = _trip()
    AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Grid Co", gnet_grid_id="gnet-1"),
        channel=Assignment.Channel.GNET,
        status=Assignment.Status.OFFERED,
    )
    body = _panel(logged_in_client, trip)

    assert ">Confirm<" not in body
    assert ">Declined<" not in body
    assert ">Withdraw<" in body
    assert "affiliate" in body.lower()


def test_panel_keeps_confirm_and_declined_for_a_manual_offer(logged_in_client):
    trip = _trip()
    services.send_offer(trip, VendorFactory(name="Manual Co"), payout=Decimal("100.00"))
    body = _panel(logged_in_client, trip)

    assert ">Confirm<" in body
    assert ">Declined<" in body
    assert ">Withdraw<" in body


# --- a GNet affiliate needs no email address ---


def test_the_send_button_is_not_gated_on_email_for_a_gnet_vendor(logged_in_client):
    """The GNet channel doesn't use email at all, so gating Send offer on a vendor
    email blocked exactly the affiliates this channel exists for."""
    trip = _trip()
    VendorFactory(name="Grid Co", gnet_grid_id="gnet-1", email="")
    body = _panel(logged_in_client, trip)

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    grid_label = next(label for label in labels if "Grid Co" in label)
    assert 'data-gnet="1"' in grid_label
    # Both the button's :disabled expression and the hint's x-show must let it through.
    assert body.count("selectedGnet") >= 3


def test_a_non_gnet_vendor_still_carries_an_empty_gnet_flag(logged_in_client):
    trip = _trip()
    VendorFactory(name="Manual Co", gnet_grid_id="", email="")
    body = _panel(logged_in_client, trip)

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    manual_label = next(label for label in labels if "Manual Co" in label)
    assert 'data-gnet=""' in manual_label


# --- preview mode must be visible in the drawer, not just in Django admin ---


def test_panel_flags_an_offer_that_preview_mode_never_sent(logged_in_client, settings):
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    trip = _trip()
    services.send_offer(trip, VendorFactory(gnet_grid_id="gnet-1"), payout=Decimal("100.00"))

    body = _panel(logged_in_client, trip)

    assert "preview" in body.lower()


def test_panel_does_not_flag_preview_for_a_manual_offer(logged_in_client, settings):
    settings.GNET_ACTIVE = False
    trip = _trip()
    services.send_offer(trip, VendorFactory(email="ops@x.example"), payout=Decimal("100.00"))

    body = _panel(logged_in_client, trip)

    assert "preview" not in body.lower()
