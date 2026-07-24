from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.vendors.factories import VendorFactory, VendorInsuranceFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db


def _login(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="agent", password="x"))


def _cover(vendor, *, days):
    VendorInsuranceFactory(vendor=vendor, expiry_date=timezone.localdate() + timedelta(days=days))


def test_active_healthy_vendor_in_roster(client, django_user_model):
    _login(client, django_user_model)
    v = VendorFactory(name="Elite Sedans")
    _cover(v, days=200)
    resp = client.get(reverse("vendor_list"))
    assert resp.status_code == 200
    assert [x.pk for x in resp.context["roster"]] == [v.pk]
    assert resp.context["attention"] == []
    assert b"Elite Sedans" in resp.content


def test_inactive_hidden_by_default(client, django_user_model):
    _login(client, django_user_model)
    VendorFactory(name="Off Duty", status=Vendor.Status.INACTIVE)
    resp = client.get(reverse("vendor_list"))
    assert b"Off Duty" not in resp.content
    assert resp.context["archived_count"] == 1


def test_archive_filter_shows_inactive(client, django_user_model):
    _login(client, django_user_model)
    VendorFactory(name="Off Duty", status=Vendor.Status.INACTIVE)
    resp = client.get(reverse("vendor_list"), {"status": "inactive"})
    assert b"Off Duty" in resp.content


def test_attention_and_roster_are_disjoint(client, django_user_model):
    _login(client, django_user_model)
    lapsing = VendorFactory(name="Crown Coach")
    _cover(lapsing, days=8)
    healthy = VendorFactory(name="Elite Sedans")
    _cover(healthy, days=200)
    resp = client.get(reverse("vendor_list"))
    assert [x.pk for x in resp.context["attention"]] == [lapsing.pk]
    assert [x.pk for x in resp.context["roster"]] == [healthy.pk]


def test_missing_coverage_active_vendor_needs_attention(client, django_user_model):
    _login(client, django_user_model)
    v = VendorFactory(name="No Coverage Co")
    resp = client.get(reverse("vendor_list"))
    assert v.pk in [x.pk for x in resp.context["attention"]]


def test_attention_sorted_worst_first(client, django_user_model):
    _login(client, django_user_model)
    soon = VendorFactory(name="Expiring")
    _cover(soon, days=25)
    lapsed = VendorFactory(name="Lapsed")
    _cover(lapsed, days=-3)
    resp = client.get(reverse("vendor_list"))
    order = [x.pk for x in resp.context["attention"]]
    assert order.index(lapsed.pk) < order.index(soon.pk)


def test_insurance_label_rendered(client, django_user_model):
    _login(client, django_user_model)
    v = VendorFactory()
    _cover(v, days=12)
    resp = client.get(reverse("vendor_list"))
    assert b"Expires in 12 days" in resp.content


def test_search_filters_by_name(client, django_user_model):
    _login(client, django_user_model)
    a = VendorFactory(name="Elite Sedans")
    _cover(a, days=200)
    b = VendorFactory(name="Budget Vans")
    _cover(b, days=200)
    resp = client.get(reverse("vendor_list"), {"q": "Elite"})
    assert b"Elite Sedans" in resp.content
    assert b"Budget Vans" not in resp.content


def test_list_requires_login(client):
    assert client.get(reverse("vendor_list")).status_code == 302


def test_list_is_query_flat(client, django_user_model, django_assert_max_num_queries):
    _login(client, django_user_model)
    for _ in range(6):
        v = VendorFactory()
        _cover(v, days=200)
    with django_assert_max_num_queries(10):
        client.get(reverse("vendor_list"))
