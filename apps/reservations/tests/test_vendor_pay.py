"""What a trip pays its vendor — the Settings standard, a per-trip factor, or a flat rate.

Settings holds the standard ("vendor keeps 65%"). Every trip starts on it, may change the
factor, or may pay a flat amount instead. Following the convention `discount` and
`gratuity` already use, there is no mode column: a flat amount (`affiliate_cost`) wins
whenever one is set, and the factor (`cost_ratio_pct`) applies otherwise.

The factor is the vendor's share of the SELL PRICE, never a margin — see
`Reservation.cost_ratio_pct`.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.drafts import DraftError, parse_draft
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import PricingConfig, Reservation

pytestmark = pytest.mark.django_db


def _trip(**kwargs) -> Reservation:
    base = {"rate": Decimal("1000"), "hours": Decimal("1"), "min_hours": Decimal("0")}
    return TransferReservationFactory(**{**base, **kwargs})


def _draft(**over) -> dict:
    stops = [{"address": "IAD"}, {"address": "The Hay-Adams"}]
    return {"tripType": "transfer", "pax": 2, "rate": 100, "stops": stops, **over}


# --- the standard ------------------------------------------------------------------


def test_a_new_trip_starts_on_the_settings_standard():
    PricingConfig.objects.update_or_create(pk=1, defaults={"default_cost_ratio_pct": 70})
    assert _trip().cost_ratio_pct == Decimal("70")


def test_a_trip_made_outside_the_editor_gets_the_standard_too():
    """The default used to be a browser pre-fill, so anything else saved at zero."""
    trip = Reservation.objects.create(lead=LeadFactory())
    assert trip.cost_ratio_pct == Decimal("65")


def test_a_trips_own_factor_is_kept():
    assert _trip(cost_ratio_pct=Decimal("60")).cost_ratio_pct == Decimal("60")


def test_changing_the_standard_later_leaves_existing_trips_alone():
    trip = _trip()
    PricingConfig.objects.update_or_create(pk=1, defaults={"default_cost_ratio_pct": 70})
    trip.save()
    assert trip.cost_ratio_pct == Decimal("65")


# --- factor, or flat ---------------------------------------------------------------


def test_by_factor_the_vendor_is_paid_their_share_of_the_sell_price():
    trip = _trip(cost_ratio_pct=Decimal("65"))
    assert trip.vendor_pay_mode == "factor"
    assert trip.vendor_pay == Decimal("650.00")


def test_a_flat_rate_wins_over_the_factor():
    trip = _trip(affiliate_cost=Decimal("580"), cost_ratio_pct=Decimal("65"))
    assert trip.vendor_pay_mode == "flat"
    assert trip.vendor_pay == Decimal("580.00")


def test_a_discount_comes_out_of_our_side_not_the_vendors():
    trip = _trip(cost_ratio_pct=Decimal("65"), discount_flat=Decimal("100"))
    assert trip.vendor_pay == Decimal("650.00")
    assert trip.quoted_profit == Decimal("250.00")


def test_gratuity_is_a_pass_through_and_pays_nobody_a_share():
    assert _trip(gratuity_pct=Decimal("20")).vendor_pay == Decimal("650.00")


def test_an_unpriced_trip_pays_nothing_and_keeps_nothing():
    trip = _trip(rate=Decimal("0"))
    assert trip.vendor_pay == Decimal("0.00")
    assert trip.quoted_profit == Decimal("0.00")
    assert trip.quoted_margin_pct == Decimal("0.00")


@pytest.mark.parametrize(
    ("kwargs", "profit", "margin"),
    [
        ({"cost_ratio_pct": Decimal("65")}, "350.00", "35.00"),
        ({"affiliate_cost": Decimal("580")}, "420.00", "42.00"),
        ({"affiliate_cost": Decimal("1200")}, "-200.00", "-20.00"),
    ],
)
def test_what_we_keep_in_either_mode(kwargs, profit, margin):
    trip = _trip(**kwargs)
    assert trip.quoted_profit == Decimal(profit)
    assert trip.quoted_margin_pct == Decimal(margin)


# --- saving from the editor --------------------------------------------------------


@pytest.mark.parametrize("ratio", [0.5, 100.01, 250])
def test_a_factor_outside_one_to_a_hundred_is_refused(ratio):
    with pytest.raises(DraftError, match="vendor share"):
        parse_draft(_draft(costRatioPct=ratio))


@pytest.mark.parametrize("ratio", [None, "", 0])
def test_a_blank_factor_means_use_the_standard(ratio):
    assert parse_draft(_draft(costRatioPct=ratio))["cost_ratio_pct"] == Decimal("0")


# --- the editor and the drawer -----------------------------------------------------


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


def test_the_editor_offers_factor_or_flat_and_what_we_keep(client, agent):
    html = client.get(reverse("lead_detail", args=[LeadFactory().pk])).content.decode()
    assert "setVendorPayMode('factor')" in html
    assert "setVendorPayMode('flat')" in html
    assert "You keep" in html
    assert "Reset to standard" in html


def test_the_editor_never_calls_the_factor_a_margin(client, agent):
    html = client.get(reverse("lead_detail", args=[LeadFactory().pk])).content.decode()
    assert "Vendor keeps" in html
    assert "Margin %" not in html
    assert "margin factor" not in html.lower()


def test_the_dispatch_drawer_prefills_the_payout_from_the_trip(client, agent):
    trip = _trip(lead=LeadFactory(status=Lead.Status.BOOKED), affiliate_cost=Decimal("580"))
    html = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert 'name="payout"' in html
    assert 'value="580.00"' in html


def test_an_unpriced_trip_prefills_no_payout(client, agent):
    trip = _trip(lead=LeadFactory(status=Lead.Status.BOOKED), rate=Decimal("0"))
    html = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert 'name="payout"' in html
    assert 'value="0.00"' not in html
