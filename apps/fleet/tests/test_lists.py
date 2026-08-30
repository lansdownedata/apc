from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.fleet.factories import (
    DriverFactory,
    RenewalFactory,
    VehicleFactory,
    VehicleRenewalFactory,
)
from apps.fleet.models import Driver, Vehicle

pytestmark = pytest.mark.django_db


def _in(days):
    return timezone.localdate() + timedelta(days=days)


def test_driver_list_requires_login(client):
    assert client.get(reverse("fleet:driver_list")).status_code == 302


def test_healthy_driver_is_in_the_roster(logged_in_client):
    d = DriverFactory(name="Marcus Bell")
    RenewalFactory(driver=d, expires_on=_in(200))
    resp = logged_in_client.get(reverse("fleet:driver_list"))
    assert resp.status_code == 200
    assert [x.pk for x in resp.context["roster"]] == [d.pk]
    assert resp.context["attention"] == []
    assert b"Marcus Bell" in resp.content
    assert b"1000" in resp.content


def test_lapsing_driver_floats_to_the_attention_strip(logged_in_client):
    lapsing = DriverFactory(name="Lapsing")
    RenewalFactory(driver=lapsing, expires_on=_in(8))
    healthy = DriverFactory(name="Healthy")
    RenewalFactory(driver=healthy, expires_on=_in(200))
    resp = logged_in_client.get(reverse("fleet:driver_list"))
    assert [x.pk for x in resp.context["attention"]] == [lapsing.pk]
    assert [x.pk for x in resp.context["roster"]] == [healthy.pk]
    assert b"Expires in 8 days" in resp.content


def test_attention_is_worst_first(logged_in_client):
    soon = DriverFactory(name="Soon")
    RenewalFactory(driver=soon, expires_on=_in(25))
    lapsed = DriverFactory(name="Lapsed")
    RenewalFactory(driver=lapsed, expires_on=_in(-3))
    order = [x.pk for x in logged_in_client.get(reverse("fleet:driver_list")).context["attention"]]
    assert order.index(lapsed.pk) < order.index(soon.pk)


def test_a_driver_with_nothing_on_file_is_healthy(logged_in_client):
    d = DriverFactory()
    resp = logged_in_client.get(reverse("fleet:driver_list"))
    assert d.pk in [x.pk for x in resp.context["roster"]]


def test_inactive_hidden_by_default_and_shown_on_request(logged_in_client):
    DriverFactory(name="Retired Ray", status=Driver.Status.INACTIVE)
    resp = logged_in_client.get(reverse("fleet:driver_list"))
    assert b"Retired Ray" not in resp.content
    assert resp.context["hidden_count"] == 1
    resp = logged_in_client.get(reverse("fleet:driver_list"), {"status": "inactive"})
    assert b"Retired Ray" in resp.content


def test_search_by_name_number_and_phone(logged_in_client):
    a = DriverFactory(name="Marcus Bell", phone="+15715550177")  # 1000
    DriverFactory(name="Dana Cole", phone="+15715550188")  # 1001
    url = reverse("fleet:driver_list")
    assert [x.pk for x in logged_in_client.get(url, {"q": "marc"}).context["roster"]] == [a.pk]
    assert [x.pk for x in logged_in_client.get(url, {"q": "1000"}).context["roster"]] == [a.pk]
    assert [x.pk for x in logged_in_client.get(url, {"q": "0177"}).context["roster"]] == [a.pk]


def test_driver_list_is_query_flat(logged_in_client, django_assert_max_num_queries):
    for _ in range(6):
        RenewalFactory(expires_on=_in(200))
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("fleet:driver_list"))


def test_empty_state_invites_the_first_driver(logged_in_client):
    body = logged_in_client.get(reverse("fleet:driver_list")).content
    assert b"No drivers yet" in body


def test_vehicle_list_shows_class_plate_and_attention(logged_in_client):
    unit = VehicleFactory(name="Unit 1", license_plate="APC-0001")
    VehicleRenewalFactory(vehicle=unit, expires_on=_in(5))
    resp = logged_in_client.get(reverse("fleet:vehicle_list"))
    assert resp.status_code == 200
    assert [x.pk for x in resp.context["attention"]] == [unit.pk]
    assert b"APC-0001" in resp.content
    assert unit.vehicle_type.name.encode() in resp.content


def test_vehicle_search_by_plate(logged_in_client):
    hit = VehicleFactory(name="Unit 1", license_plate="ZZZ-9999")
    VehicleFactory(name="Unit 2", license_plate="AAA-1111")
    resp = logged_in_client.get(reverse("fleet:vehicle_list"), {"q": "zzz"})
    assert [x.pk for x in resp.context["roster"]] == [hit.pk]


def test_inactive_vehicles_hidden_by_default(logged_in_client):
    VehicleFactory(name="Sold Unit", status=Vehicle.Status.INACTIVE)
    assert b"Sold Unit" not in logged_in_client.get(reverse("fleet:vehicle_list")).content


def test_nav_marks_fleet_active(logged_in_client):
    assert logged_in_client.get(reverse("fleet:driver_list")).context["nav"] == "fleet"
