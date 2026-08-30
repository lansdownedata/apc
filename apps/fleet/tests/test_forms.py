from datetime import timedelta

import pytest
from django.utils import timezone

from apps.fleet.factories import DriverFactory, RenewalFactory, RenewalTypeFactory, VehicleFactory
from apps.fleet.forms import DriverForm, RenewalForm, RenewalTypeForm, VehicleForm
from apps.fleet.models import RenewalType
from apps.leads.factories import VehicleTypeFactory

pytestmark = pytest.mark.django_db


def test_driver_form_never_exposes_the_number():
    assert "driver_number" not in DriverForm().fields


def test_driver_form_requires_only_the_name():
    form = DriverForm({"name": "Marcus Bell", "status": "active"})
    assert form.is_valid(), form.errors


def test_vehicle_form_offers_active_types_plus_the_current_one():
    retired = VehicleTypeFactory(name="Retired Van", active=False)
    live = VehicleTypeFactory(name="Live SUV")
    assert live in VehicleForm().fields["vehicle_type"].queryset
    assert retired not in VehicleForm().fields["vehicle_type"].queryset
    unit = VehicleFactory(vehicle_type=retired)
    assert retired in VehicleForm(instance=unit).fields["vehicle_type"].queryset


def test_renewal_form_only_offers_types_for_the_subject_kind():
    licence = RenewalType.objects.get(name="Driver's license")
    registration = RenewalType.objects.get(name="Registration")
    form = RenewalForm(applies_to=RenewalType.AppliesTo.DRIVER)
    qs = form.fields["renewal_type"].queryset
    assert licence in qs and registration not in qs


def test_renewal_form_rejects_a_type_for_the_other_subject_kind():
    registration = RenewalType.objects.get(name="Registration")
    form = RenewalForm(
        {"renewal_type": registration.pk, "expires_on": str(timezone.localdate())},
        applies_to=RenewalType.AppliesTo.DRIVER,
    )
    assert not form.is_valid()
    assert "renewal_type" in form.errors


def test_renewal_form_hides_inactive_types_but_keeps_the_instances_own():
    retired = RenewalTypeFactory(active=False)
    assert retired not in RenewalForm(applies_to="driver").fields["renewal_type"].queryset
    row = RenewalFactory(renewal_type=retired)
    assert retired in RenewalForm(instance=row, applies_to="driver").fields["renewal_type"].queryset
    assert (
        retired
        in RenewalForm(applies_to="driver", keep_type_id=retired.pk).fields["renewal_type"].queryset
    )


def test_renewal_form_rejects_expiry_before_issue():
    licence = RenewalType.objects.get(name="Driver's license")
    today = timezone.localdate()
    form = RenewalForm(
        {
            "renewal_type": licence.pk,
            "issued_on": str(today),
            "expires_on": str(today - timedelta(days=1)),
        },
        applies_to="driver",
    )
    assert not form.is_valid()
    assert "before" in str(form.errors)


def test_renewal_form_requires_only_type_and_expiry():
    licence = RenewalType.objects.get(name="Driver's license")
    form = RenewalForm(
        {"renewal_type": licence.pk, "expires_on": str(timezone.localdate())},
        applies_to="driver",
    )
    assert form.is_valid(), form.errors
    DriverFactory()  # keeps the import honest — the form itself never needs a subject


def test_renewal_type_form_fields_and_validity():
    form = RenewalTypeForm(
        {"name": "Medical card", "applies_to": "driver", "sort_order": 3, "active": "on"}
    )
    assert list(form.fields) == ["name", "applies_to", "sort_order", "active"]
    assert form.is_valid(), form.errors
    assert form.save().applies_to == RenewalType.AppliesTo.DRIVER
