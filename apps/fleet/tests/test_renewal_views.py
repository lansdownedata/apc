from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.fleet.factories import DriverFactory, RenewalFactory, RenewalTypeFactory, VehicleFactory
from apps.fleet.models import Renewal, RenewalType

pytestmark = pytest.mark.django_db


def _in(days):
    return timezone.localdate() + timedelta(days=days)


def test_add_driver_renewal_with_a_scan(logged_in_client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    d = DriverFactory()
    licence = RenewalType.objects.get(name="Driver's license")
    scan = SimpleUploadedFile("licence.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = logged_in_client.post(
        reverse("fleet:driver_renewal_create", args=[d.pk]),
        {
            "renewal_type": licence.pk,
            "reference": "D-4471",
            "issued_on": str(timezone.localdate()),
            "expires_on": str(_in(365)),
            "document": scan,
        },
    )
    assert resp.status_code == 302 and resp.url == reverse("fleet:driver_detail", args=[d.pk])
    row = Renewal.objects.get(driver=d)
    assert row.reference == "D-4471" and row.vehicle is None
    assert row.document.name.startswith("renewals/")


def test_add_vehicle_renewal_refuses_a_driver_type(logged_in_client):
    v = VehicleFactory()
    licence = RenewalType.objects.get(name="Driver's license")
    resp = logged_in_client.post(
        reverse("fleet:vehicle_renewal_create", args=[v.pk]),
        {"renewal_type": licence.pk, "expires_on": str(_in(30))},
    )
    assert resp.status_code == 200  # re-rendered with errors
    assert Renewal.objects.count() == 0


def test_add_vehicle_renewal(logged_in_client):
    v = VehicleFactory()
    registration = RenewalType.objects.get(name="Registration")
    logged_in_client.post(
        reverse("fleet:vehicle_renewal_create", args=[v.pk]),
        {"renewal_type": registration.pk, "expires_on": str(_in(30))},
    )
    row = Renewal.objects.get(vehicle=v)
    assert row.driver is None and row.renewal_type == registration


def test_edit_changes_the_row_in_place(logged_in_client):
    row = RenewalFactory(reference="TYPO")
    logged_in_client.post(
        reverse("fleet:renewal_edit", args=[row.pk]),
        {
            "renewal_type": row.renewal_type.pk,
            "reference": "FIXED",
            "expires_on": str(row.expires_on),
        },
    )
    row.refresh_from_db()
    assert row.reference == "FIXED"
    assert Renewal.objects.count() == 1


def test_renew_prefills_and_creates_a_new_row(logged_in_client):
    old = RenewalFactory(reference="D-4471", expires_on=_in(5))
    page = logged_in_client.get(reverse("fleet:renewal_renew", args=[old.pk]))
    assert page.status_code == 200
    form = page.context["form"]
    assert form.initial["reference"] == "D-4471"
    assert form.initial["renewal_type"] == old.renewal_type.pk
    assert form.initial["issued_on"] == timezone.localdate()

    logged_in_client.post(
        reverse("fleet:renewal_renew", args=[old.pk]),
        {"renewal_type": old.renewal_type.pk, "reference": "D-4471", "expires_on": str(_in(370))},
    )
    assert Renewal.objects.filter(driver=old.driver).count() == 2
    old.refresh_from_db()
    assert old.expires_on == _in(5)  # the old row is untouched history


def test_renew_keeps_a_retired_type_selectable(logged_in_client):
    retired = RenewalTypeFactory(active=False)
    old = RenewalFactory(renewal_type=retired)
    page = logged_in_client.get(reverse("fleet:renewal_renew", args=[old.pk]))
    assert retired in page.context["form"].fields["renewal_type"].queryset


def test_delete_is_post_only_and_removes_the_row(logged_in_client):
    row = RenewalFactory()
    url = reverse("fleet:renewal_delete", args=[row.pk])
    assert logged_in_client.get(url).status_code == 405
    resp = logged_in_client.post(url)
    assert resp.status_code == 302
    assert not Renewal.objects.filter(pk=row.pk).exists()


def test_detail_page_carries_the_actions(logged_in_client):
    row = RenewalFactory()
    body = logged_in_client.get(
        reverse("fleet:driver_detail", args=[row.driver.pk])
    ).content.decode()
    assert reverse("fleet:renewal_renew", args=[row.pk]) in body
    assert reverse("fleet:renewal_edit", args=[row.pk]) in body
    assert reverse("fleet:renewal_delete", args=[row.pk]) in body
    assert reverse("fleet:driver_renewal_create", args=[row.driver.pk]) in body
    assert "$store.modal.confirm" in body  # delete goes through the shared modal


def test_renewal_views_require_login(client):
    row = RenewalFactory()
    assert client.get(reverse("fleet:renewal_edit", args=[row.pk])).status_code == 302
