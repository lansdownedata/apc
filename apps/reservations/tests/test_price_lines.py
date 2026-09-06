"""What the customer sees of a price (spec 2026-09-05 §4).

Client rule: no gratuity and no other line item -> show ONE total. Fees or gratuity on top
-> show them separately, above the total.

Today `quote.html` labels `line_total` "Vehicle Subtotal" — and `line_total` already includes
gratuity, so the label is wrong in both directions: a total called a subtotal when there are
no extras, and an invisible gratuity when there are.
"""

from decimal import Decimal

import pytest

from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _res(**kwargs):
    return ReservationFactory(rate=Decimal("1538.50"), hours=Decimal("1"), min_hours=0, **kwargs)


# --- has_extras -------------------------------------------------------------
def test_a_plain_price_has_no_extras():
    assert _res().has_extras is False


def test_gratuity_is_an_extra():
    assert _res(gratuity_pct=Decimal("20")).has_extras is True


def test_a_discount_is_an_extra():
    assert _res(discount_flat=Decimal("50")).has_extras is True


def test_a_zero_gratuity_percent_is_not_an_extra():
    assert _res(gratuity_pct=Decimal("0")).has_extras is False


# --- price_lines ------------------------------------------------------------
def test_a_flat_price_has_no_lines_to_show():
    """Empty means the caller shows `line_total` alone, labelled Total."""
    assert _res().price_lines() == []


def test_gratuity_renders_as_its_own_line():
    lines = _res(gratuity_pct=Decimal("20")).price_lines()

    labels = [line.label for line in lines]
    assert "Base fare" in labels
    assert any("Gratuity" in label for label in labels)
    assert lines[-1].amount == Decimal("307.70")


def test_a_discount_renders_negative():
    lines = _res(discount_flat=Decimal("50")).price_lines()

    discount = [line for line in lines if "Discount" in line.label][0]
    assert discount.amount == Decimal("-50.00")


@pytest.mark.parametrize(
    ("pct", "label"),
    [("20", "Gratuity (20%)"), ("20.00", "Gratuity (20%)"), ("12.50", "Gratuity (12.5%)")],
)
def test_the_percent_is_named_on_a_percentage_gratuity(pct, label):
    """ "Gratuity" alone invites "on what?" — the customer should be able to check it.

    Exact labels, because the obvious ways to trim trailing zeros are all wrong here:
    `.normalize()` renders 20.00 as "2E+1", and a bare `.rstrip("0")` renders 20 as "2".
    """
    lines = _res(gratuity_pct=Decimal(pct)).price_lines()

    assert lines[-1].label == label


def test_a_flat_gratuity_names_no_percentage():
    lines = _res(gratuity_flat=Decimal("120")).price_lines()

    assert lines[-1].label == "Gratuity"


def test_lines_appear_in_reading_order():
    lines = _res(discount_pct=Decimal("10"), gratuity_pct=Decimal("20")).price_lines()

    assert [line.label.split(" ")[0] for line in lines] == ["Base", "Discount", "Gratuity"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"gratuity_pct": Decimal("20")},
        {"discount_flat": Decimal("50")},
        {"discount_pct": Decimal("10"), "gratuity_pct": Decimal("20")},
        {"gratuity_flat": Decimal("120")},
        {"discount_flat": Decimal("50"), "gratuity_flat": Decimal("120")},
    ],
)
def test_the_lines_always_sum_to_the_line_total(kwargs):
    """The test that catches a display drifting away from the money."""
    r = _res(**kwargs)

    assert sum(line.amount for line in r.price_lines()) == r.line_total
