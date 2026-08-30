from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.fleet.factories import (
    DriverFactory,
    RenewalFactory,
    RenewalTypeFactory,
    VehicleFactory,
    VehicleRenewalFactory,
)
from apps.fleet.models import Driver, Renewal, RenewalType, Vehicle

pytestmark = pytest.mark.django_db


def _in(days: int):
    return timezone.localdate() + timedelta(days=days)


# --- the type catalog ---


def test_default_types_are_seeded_by_migration():
    names = set(RenewalType.objects.values_list("name", "applies_to"))
    assert ("Driver's license", "driver") in names
    assert {
        ("Registration", "vehicle"),
        ("State inspection", "vehicle"),
        ("Airport permit", "vehicle"),
    } <= names


def test_type_name_is_unique_per_subject_kind_case_insensitively():
    # objects.create, not the factory: its get_or_create lookup is itself case-insensitive
    # on MySQL's default collation and would just return the first row.
    RenewalType.objects.create(name="Medical card", applies_to=RenewalType.AppliesTo.DRIVER)
    with pytest.raises(IntegrityError), transaction.atomic():
        RenewalType.objects.create(name="MEDICAL CARD", applies_to=RenewalType.AppliesTo.DRIVER)
    # the same name is fine for the other subject kind
    RenewalType.objects.create(name="Medical card", applies_to=RenewalType.AppliesTo.VEHICLE)


def test_a_type_with_records_cannot_be_deleted():
    r = RenewalFactory()
    with pytest.raises(ProtectedError):
        r.renewal_type.delete()


# --- the status ramp (mirrors VendorInsurance exactly) ---


@pytest.mark.parametrize(
    "days,status",
    [
        (31, "valid"),
        (30, "expiring"),
        (16, "expiring"),
        (15, "urgent"),
        (11, "urgent"),
        (10, "critical"),
        (0, "critical"),
        (-1, "expired"),
    ],
)
def test_status_ramp_boundaries(days, status):
    assert RenewalFactory(expires_on=_in(days)).status == status


def test_labels():
    assert RenewalFactory(expires_on=_in(-3)).label == "Lapsed 3 days ago"
    assert RenewalFactory(expires_on=_in(-1)).label == "Lapsed 1 day ago"
    assert RenewalFactory(expires_on=_in(0)).label == "Expires today"
    assert RenewalFactory(expires_on=_in(8)).label == "Expires in 8 days"
    valid = RenewalFactory(expires_on=_in(200))
    assert valid.label == f"Valid · exp {valid.expires_on:%b '%y}"


# --- exactly one subject ---


def test_a_renewal_needs_exactly_one_subject():
    t = RenewalTypeFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        Renewal.objects.create(renewal_type=t, expires_on=_in(10))
    with pytest.raises(IntegrityError), transaction.atomic():
        Renewal.objects.create(
            renewal_type=t, driver=DriverFactory(), vehicle=VehicleFactory(), expires_on=_in(10)
        )


def test_subject_is_the_driver_or_the_vehicle():
    r = RenewalFactory()
    assert r.subject == r.driver
    v = VehicleRenewalFactory()
    assert v.subject == v.vehicle


# --- current record + roll-up ---


def test_current_is_the_latest_expiry_per_type_and_history_is_ignored():
    driver = DriverFactory()
    licence = RenewalType.objects.get(name="Driver's license")
    RenewalFactory(driver=driver, renewal_type=licence, expires_on=_in(-40))  # old, lapsed
    fresh = RenewalFactory(driver=driver, renewal_type=licence, expires_on=_in(300))
    driver = Driver.objects.prefetch_related("renewals__renewal_type").get(pk=driver.pk)
    assert [r.pk for r in driver.current_renewals] == [fresh.pk]
    assert driver.renewal_status == "valid"


def test_rollup_is_the_worst_across_current_records():
    driver = DriverFactory()
    RenewalFactory(driver=driver, expires_on=_in(200))
    RenewalFactory(driver=driver, renewal_type=RenewalTypeFactory(), expires_on=_in(12))
    driver = Driver.objects.prefetch_related("renewals__renewal_type").get(pk=driver.pk)
    assert driver.renewal_status == "urgent"
    assert driver.needs_attention is True
    summary = driver.renewal_summary()
    assert summary["label"] == "Expires in 12 days"
    assert summary["type"]  # names the governing record's type


def test_no_records_is_none_and_not_attention():
    v = VehicleFactory()
    assert v.renewal_status == "none"
    assert v.needs_attention is False
    assert v.renewal_summary()["label"] == "Nothing on file"


def test_current_records_follow_type_sort_order():
    driver = DriverFactory()
    b = RenewalTypeFactory(name="B type", sort_order=2)
    a = RenewalTypeFactory(name="A type", sort_order=1)
    RenewalFactory(driver=driver, renewal_type=b, expires_on=_in(100))
    RenewalFactory(driver=driver, renewal_type=a, expires_on=_in(100))
    driver = Driver.objects.prefetch_related("renewals__renewal_type").get(pk=driver.pk)
    assert [r.renewal_type.pk for r in driver.current_renewals] == [a.pk, b.pk]


def test_a_deactivated_type_still_counts():
    driver = DriverFactory()
    retired = RenewalTypeFactory(active=False)
    RenewalFactory(driver=driver, renewal_type=retired, expires_on=_in(-2))
    driver = Driver.objects.prefetch_related("renewals__renewal_type").get(pk=driver.pk)
    assert driver.renewal_status == "expired"


def test_vehicle_defaults_and_str():
    v = VehicleFactory(name="Unit 1")
    assert v.status == Vehicle.Status.ACTIVE
    assert str(v) == "Unit 1"
