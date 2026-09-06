"""Pricing a trip from the affiliate's rate (APC-26 step 3).

Spec: docs/specs/2026-09-05-cost-based-pricing-design.md §3.1-3.2.

The client's method is `price = affiliate_cost / cost_ratio`. His "65%" is the affiliate's
share of the sell price, NOT a margin — his gross margin is the complement, 35%. Every name
in here says `cost_ratio`, deliberately: read as a margin, the textbook formula
`cost / (1 - 0.65)` gives $2,857 instead of $1,538.50.
"""

from decimal import Decimal

import pytest

from apps.reservations.factories import HourlyReservationFactory, TransferReservationFactory
from apps.reservations.services import solve_rate_from_cost

pytestmark = pytest.mark.django_db


def _priced(**kwargs):
    base = {
        "rate": Decimal("0"),
        "hours": Decimal("1"),
        "min_hours": Decimal("0"),
        "affiliate_cost": Decimal("1000"),
        "cost_ratio_pct": Decimal("65"),
    }
    return TransferReservationFactory(**{**base, **kwargs})


# --- target price -----------------------------------------------------------
def test_target_price_is_cost_over_ratio():
    assert _priced().target_price == Decimal("1538.46")


def test_no_cost_or_no_ratio_means_no_target():
    assert _priced(affiliate_cost=Decimal("0")).target_price == Decimal("0.00")
    assert _priced(cost_ratio_pct=Decimal("0")).target_price == Decimal("0.00")


def test_a_hundred_percent_ratio_prices_at_cost():
    """Zero margin is a legal deal, not an error."""
    assert _priced(cost_ratio_pct=Decimal("100")).target_price == Decimal("1000.00")


def test_a_lower_ratio_is_a_higher_price():
    """The lever runs backwards from intuition — the trap spec 1.1 is about."""
    assert _priced(cost_ratio_pct=Decimal("60")).target_price == Decimal("1666.67")
    assert _priced(cost_ratio_pct=Decimal("70")).target_price == Decimal("1428.57")


# --- profit and margin ------------------------------------------------------
def test_profit_and_margin_on_the_worked_example():
    r = _priced(rate=Decimal("1538.50"))
    assert r.quoted_profit == Decimal("538.50")
    assert r.quoted_margin_pct == Decimal("35.00")


def test_profit_reads_the_real_base_not_the_target():
    """Hand-edit the rate down and the profit must fall with it — showing the ideal would
    quietly overstate what we make."""
    r = _priced(rate=Decimal("1200.00"))
    assert r.quoted_profit == Decimal("200.00")


def test_a_discount_cuts_our_margin_not_the_affiliates():
    r = _priced(rate=Decimal("1538.50"), discount_flat=Decimal("100"))
    assert r.quoted_profit == Decimal("438.50")
    assert r.affiliate_cost == Decimal("1000.00")


def test_margin_excludes_gratuity():
    """Gratuity is a pass-through to the driver. Counting it as margin would inflate the
    number by exactly what we don't keep."""
    plain = _priced(rate=Decimal("1538.50"))
    tipped = _priced(rate=Decimal("1538.50"), gratuity_pct=Decimal("20"))
    assert tipped.quoted_margin_pct == plain.quoted_margin_pct == Decimal("35.00")
    assert tipped.line_total > plain.line_total


def test_margin_on_a_zero_base_does_not_divide():
    assert _priced(rate=Decimal("0")).quoted_margin_pct == Decimal("0.00")


def test_a_cost_entered_before_a_rate_is_not_a_loss():
    """Cost captured, rate not applied yet — nothing is quoted, so report nothing rather
    than the full cost as a loss."""
    assert _priced(rate=Decimal("0")).quoted_profit == Decimal("0.00")


def test_an_underpriced_trip_really_does_report_a_loss():
    """The guard above must not swallow a real one."""
    r = _priced(rate=Decimal("800"))
    assert r.quoted_profit == Decimal("-200.00")
    assert r.quoted_margin_pct == Decimal("-25.00")


def test_an_uncosted_trip_reports_no_profit():
    """A trip priced the old way has no affiliate cost — don't claim its whole fare is
    profit."""
    r = _priced(rate=Decimal("500"), affiliate_cost=Decimal("0"))
    assert r.quoted_profit == Decimal("0.00")
    assert r.quoted_margin_pct == Decimal("0.00")


# --- solving for the rate ---------------------------------------------------
def test_transfer_rate_is_the_dime_rounded_price():
    r = _priced()
    rate = solve_rate_from_cost(r)
    assert rate == Decimal("1538.50")
    r.rate = rate
    assert r.line_total == Decimal("1538.50")


def test_hourly_rate_divides_the_target_and_never_prices_below_it():
    r = HourlyReservationFactory(
        rate=Decimal("0"),
        hours=Decimal("6"),
        min_hours=Decimal("4"),
        affiliate_cost=Decimal("1000"),
        cost_ratio_pct=Decimal("65"),
    )
    r.rate = solve_rate_from_cost(r)
    assert r.line_total >= r.target_price


def test_the_rate_card_minimum_is_what_divides_when_there_is_no_override():
    r = HourlyReservationFactory(
        rate=Decimal("0"),
        hours=Decimal("0"),
        min_hours=Decimal("4"),
        affiliate_cost=Decimal("1000"),
        cost_ratio_pct=Decimal("65"),
    )
    assert r.billed_hours == Decimal("4")
    assert solve_rate_from_cost(r) == Decimal("384.70")  # 1538.46 / 4 = 384.615 -> up


def test_no_billable_hours_means_no_rate_to_solve():
    r = _priced(hours=Decimal("0"), min_hours=Decimal("0"))
    assert solve_rate_from_cost(r) is None


def test_no_cost_means_nothing_to_solve():
    assert solve_rate_from_cost(_priced(affiliate_cost=Decimal("0"))) is None
    assert solve_rate_from_cost(_priced(cost_ratio_pct=Decimal("0"))) is None


def test_solving_twice_gives_the_same_rate():
    """Re-opening a saved trip and re-applying must not walk the price up a dime a time."""
    r = _priced()
    first = solve_rate_from_cost(r)
    r.rate = first
    assert solve_rate_from_cost(r) == first
