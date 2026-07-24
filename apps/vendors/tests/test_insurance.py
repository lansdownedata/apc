from datetime import timedelta

import pytest
from django.utils import timezone

from apps.vendors.factories import VendorFactory
from apps.vendors.models import VendorInsurance

pytestmark = pytest.mark.django_db


def _policy(vendor, *, days_to_expiry):
    today = timezone.localdate()
    return VendorInsurance.objects.create(
        vendor=vendor,
        insurer="Acme Mutual",
        policy_number="P-1",
        coverage_amount=1_000_000,
        effective_date=today - timedelta(days=365),
        expiry_date=today + timedelta(days=days_to_expiry),
    )


@pytest.mark.parametrize(
    "days,expected",
    [
        (60, "valid"),
        (31, "valid"),
        (30, "expiring"),  # amber, upper edge
        (16, "expiring"),
        (15, "urgent"),  # orange, upper edge
        (11, "urgent"),
        (10, "critical"),  # red, upper edge
        (1, "critical"),
        (0, "critical"),  # expires today is still not yet expired
        (-1, "expired"),  # solid red
    ],
)
def test_status_ramp(days, expected):
    assert _policy(VendorFactory(), days_to_expiry=days).status == expected


def test_days_until_expiry():
    assert _policy(VendorFactory(), days_to_expiry=12).days_until_expiry == 12


def test_vendor_rollup_is_worst_case():
    vendor = VendorFactory()
    _policy(vendor, days_to_expiry=60)  # valid
    _policy(vendor, days_to_expiry=12)  # urgent
    _policy(vendor, days_to_expiry=-1)  # expired
    assert vendor.insurance_status == "expired"


def test_vendor_rollup_none_when_no_policies():
    assert VendorFactory().insurance_status == "none"


def test_needs_attention_true_for_expiring_and_missing():
    lapsing = VendorFactory()
    _policy(lapsing, days_to_expiry=5)
    assert lapsing.needs_attention is True
    assert VendorFactory().needs_attention is True  # no coverage on file


def test_needs_attention_false_when_valid():
    ok = VendorFactory()
    _policy(ok, days_to_expiry=200)
    assert ok.needs_attention is False


def test_summary_labels():
    none = VendorFactory().insurance_summary()
    assert none["status"] == "none" and none["label"] == "No coverage on file"

    lapsed = VendorFactory()
    _policy(lapsed, days_to_expiry=-5)
    s = lapsed.insurance_summary()
    assert s["status"] == "expired" and s["label"] == "Lapsed 5 days ago"

    soon = VendorFactory()
    _policy(soon, days_to_expiry=12)
    assert soon.insurance_summary()["label"] == "Expires in 12 days"

    good = VendorFactory()
    _policy(good, days_to_expiry=200)
    assert good.insurance_summary()["label"].startswith("Valid · exp ")
