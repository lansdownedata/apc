"""APC-24 — the dispatch board view: week / range / program filters, day sub-headers."""

from datetime import time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.dispatch import services
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations import groups
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

DAY = timezone.localdate() + timedelta(days=30)


def _trip(day=DAY, **kw):
    lead = kw.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kw.setdefault("pickup_time", time(9, 0))
    return ReservationFactory(lead=lead, pickup_date=day, **kw)


def test_week_view_shows_seven_days_of_trips(logged_in_client):
    monday = DAY - timedelta(days=DAY.weekday())
    _trip(monday)
    _trip(monday + timedelta(days=6))
    _trip(monday + timedelta(days=9))  # next week — excluded

    resp = logged_in_client.get(reverse("dispatch_board"), {"view": "week", "day": DAY.isoformat()})

    assert resp.context["filters"].view == "week"
    assert len(resp.context["trips"]) == 2


def test_range_view_spans_the_requested_dates(logged_in_client):
    _trip(DAY)
    _trip(DAY + timedelta(days=4))
    _trip(DAY + timedelta(days=40))

    resp = logged_in_client.get(
        reverse("dispatch_board"),
        {"view": "range", "start": DAY.isoformat(), "end": (DAY + timedelta(days=5)).isoformat()},
    )

    assert len(resp.context["trips"]) == 2


def test_multi_day_view_groups_trips_under_a_day_header(logged_in_client):
    _trip(DAY)
    _trip(DAY + timedelta(days=1))

    resp = logged_in_client.get(reverse("dispatch_board"), {"view": "week", "day": DAY.isoformat()})

    groups_ctx = resp.context["day_groups"]
    assert [d for d, _, _ in groups_ctx] == sorted(d for d, _, _ in groups_ctx)
    assert len(groups_ctx) == 2
    body = resp.content.decode()
    assert DAY.strftime("%A") in body or DAY.strftime("%a") in body  # weekday sub-header


def test_day_view_has_no_day_groups(logged_in_client):
    _trip(DAY)

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert resp.context["day_groups"] is None


def test_vehicle_filter_narrows_the_board_and_shows_a_chip(logged_in_client):
    sedan = VehicleTypeFactory(name="Sedan")
    coach = VehicleTypeFactory(name="56-Passenger Coach")
    _trip(vehicle=sedan)
    _trip(vehicle=coach)

    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "vehicle": sedan.pk}
    )

    assert len(resp.context["trips"]) == 1
    assert b"Sedan" in resp.content


def test_customer_filter_narrows_the_board(logged_in_client):
    alice = ContactFactory(name="Alice Argent")
    _trip(lead=LeadFactory(status=Lead.Status.BOOKED, contact=alice))
    _trip()

    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "customer": alice.pk}
    )

    assert len(resp.context["trips"]) == 1


def test_program_filter_shows_one_linked_set_across_its_dates(logged_in_client):
    anchor = _trip(DAY)
    groups.set_group_size(anchor, 3)
    groups.copy_to_dates(anchor, [DAY + timedelta(days=1)])  # a next-day copy (ungrouped)
    _trip(DAY)  # unrelated

    resp = logged_in_client.get(
        reverse("dispatch_board"),
        {"view": "week", "day": DAY.isoformat(), "group": str(anchor.group_key)},
    )

    assert len(resp.context["trips"]) == 3  # copy_to_dates leaves copies ungrouped


def test_attention_strip_counts_span_the_whole_window_not_the_coverage_filter(logged_in_client):
    covered = _trip(DAY)
    services.assign_direct(covered, VendorFactory(), payout=1)
    _trip(DAY + timedelta(days=1))  # uncovered

    resp = logged_in_client.get(
        reverse("dispatch_board"),
        {"view": "week", "day": DAY.isoformat(), "f": "uncovered"},
    )

    assert resp.context["counts"]["confirmed"] == 1
    assert resp.context["counts"]["uncovered"] == 1
    assert len(resp.context["trips"]) == 1  # grid narrowed to uncovered


def test_view_switch_and_nav_links_carry_the_active_filters(logged_in_client):
    sedan = VehicleTypeFactory(name="Sedan")
    _trip(vehicle=sedan)

    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "vehicle": sedan.pk}
    )
    body = resp.content.decode()

    assert f"vehicle={sedan.pk}" in resp.context["filters"].next_url
    assert "view=week" in body  # a switch link exists
    assert f"vehicle={sedan.pk}" in body


def test_day_view_still_paged_by_day_with_the_legacy_param(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert resp.context["filters"].start == DAY
    assert resp.context["filters"].next_url == f"?day={(DAY + timedelta(days=1)).isoformat()}"


def test_week_query_count_stays_flat(logged_in_client, django_assert_max_num_queries):
    monday = DAY - timedelta(days=DAY.weekday())
    for offset in range(7):
        for hour in (8, 14):
            t = _trip(monday + timedelta(days=offset), pickup_time=time(hour, 0))
            services.assign_direct(t, VendorFactory(), payout=1)

    with django_assert_max_num_queries(16):
        logged_in_client.get(reverse("dispatch_board"), {"view": "week", "day": DAY.isoformat()})


def test_board_requires_login(client):
    resp = client.get(reverse("dispatch_board"))

    assert resp.status_code == 302
    assert "/portal/login/" in resp["Location"]
