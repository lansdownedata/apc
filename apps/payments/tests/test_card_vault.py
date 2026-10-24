"""APC-47 — the customer-level card vault.

Cards belong to the customer: one Stripe customer per contact, saved cards with brand,
last four and expiry only, and a Remove that can never make a balance charge fail.
Stripe is mocked throughout; nothing here calls it for real.
"""

from datetime import date
from unittest.mock import patch

import pytest

from apps.contacts.factories import ContactFactory
from apps.payments.models import SavedCard

pytestmark = pytest.mark.django_db


def _card(contact=None, **kw):
    kw.setdefault("stripe_payment_method_id", f"pm_{SavedCard.objects.count() + 1}")
    kw.setdefault("brand", "visa")
    kw.setdefault("last4", "4242")
    kw.setdefault("exp_month", 8)
    kw.setdefault("exp_year", 2028)
    return SavedCard.objects.create(contact=contact or ContactFactory(), **kw)


# --- Task 1: the model ----------------------------------------------------------------


def test_a_contact_has_no_stripe_customer_until_one_is_needed():
    assert ContactFactory().stripe_customer_id == ""


def test_the_expiry_label_is_month_slash_two_digit_year():
    assert _card(exp_month=8, exp_year=2028).exp_label == "08/28"


@pytest.mark.parametrize(
    ("today", "exp", "soon", "expired"),
    [
        (date(2026, 10, 24), (11, 2026), True, False),  # ends next month: flag it
        (date(2026, 10, 24), (10, 2026), True, False),  # ends this month: still valid
        (date(2026, 10, 24), (1, 2027), False, False),  # three months out: fine
        (date(2026, 10, 24), (9, 2026), False, True),  # last month: expired, not "soon"
    ],
)
def test_expiry_flags(today, exp, soon, expired):
    card = _card(exp_month=exp[0], exp_year=exp[1])

    with patch("apps.payments.models.timezone.localdate", return_value=today):
        assert (card.expires_soon, card.expired) == (soon, expired)


def test_only_brand_last_four_and_expiry_are_stored():
    fields = {f.name for f in SavedCard._meta.get_fields() if getattr(f, "concrete", False)}

    assert not {"number", "pan", "cvc", "cvv", "fingerprint"} & fields
    assert {"brand", "last4", "exp_month", "exp_year"} <= fields
