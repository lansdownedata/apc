from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.vendors.factories import (
    VendorDocumentFactory,
    VendorDriverFactory,
    VendorFactory,
    VendorInsuranceFactory,
)

pytestmark = pytest.mark.django_db


def _login(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="agent", password="x"))


def _cover(vendor, *, days):
    VendorInsuranceFactory(vendor=vendor, expiry_date=timezone.localdate() + timedelta(days=days))


def test_detail_shows_children(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory(name="Elite Sedans")
    VendorDriverFactory(vendor=vendor, name="Sam Root")
    VendorInsuranceFactory(vendor=vendor, insurer="Acme Mutual")
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert resp.status_code == 200
    assert b"Sam Root" in resp.content
    assert b"Acme Mutual" in resp.content


def test_detail_404_for_missing(client, django_user_model):
    _login(client, django_user_model)
    assert client.get(reverse("vendor_detail", args=[999999])).status_code == 404


def test_insurance_section_precedes_drivers(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    body = client.get(reverse("vendor_detail", args=[vendor.pk])).content.decode()
    assert body.index(">Insurance<") < body.index(">Drivers<")


def test_banner_shown_when_expiring(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    _cover(vendor, days=12)
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert b"Renew before assigning" in resp.content


def test_banner_absent_when_valid(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    _cover(vendor, days=200)
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert b"Renew before assigning" not in resp.content
    assert resp.context["vendor"].banner is None


def test_missing_coverage_banner_directs_next_action(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert b"Add a policy to clear this vendor for assignments." in resp.content


def test_banner_shown_when_expired(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    _cover(vendor, days=-5)
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert b"Renew before assigning trips to this vendor." in resp.content
    assert b"Renew before assigning new trips to this vendor." not in resp.content


def test_documents_query_flat_across_distinct_uploaders(
    client, django_user_model, django_assert_max_num_queries
):
    _login(client, django_user_model)
    vendor = VendorFactory()
    for i in range(3):
        uploader = django_user_model.objects.create_user(username=f"up-{i}", password="x")
        VendorDocumentFactory(vendor=vendor, label=f"Doc {i}", uploaded_by=uploader)
    with django_assert_max_num_queries(12):
        resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert resp.status_code == 200
