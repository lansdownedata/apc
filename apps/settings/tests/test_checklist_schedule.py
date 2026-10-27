"""Settings -> Checklist schedule: the form page and the example-line endpoint."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.settings import views
from apps.tasks import schedule, services
from apps.tasks.models import Task, TaskSchedule

pytestmark = pytest.mark.django_db

FIXED = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)  # a Monday


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def _defaults() -> dict:
    data = {}
    for row in schedule.ROWS:
        amount, unit = schedule.code_default(row.key, row.variant)
        data[f"amount__{row.key}__{row.variant}"] = amount
        data[f"unit__{row.key}__{row.variant}"] = unit
    return data


def test_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("checklist_schedule")).status_code == 403
    assert client.get(reverse("checklist_schedule_preview")).status_code == 403


def test_lists_every_row_with_defaults(client):
    _owner(client)
    body = client.get(reverse("checklist_schedule")).content.decode()
    for text in ("Deposit received", "Farmed-in orders", "Insurance renewal", "Booking"):
        assert text in body
    assert 'name="amount__deposit_received__standard"' in body
    assert 'value="72"' in body
    assert "Opens when the balance is charged (30 days before pickup)." in body


def test_post_saves_and_reports_redated(client):
    _owner(client)
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        lead = LeadFactory(status=Lead.Status.BOOKED)
        ReservationFactory(
            lead=lead,
            pickup_timezone="America/New_York",
            hours=Decimal("1"),
            pickup_date=timezone.localdate() + timedelta(days=20),
            pickup_time=time(9, 0),
        )
        services.ensure_tasks(lead)
        assert Task.objects.filter(lead=lead, kind="affiliate_assigned").exists()
        data = _defaults()
        data["amount__affiliate_assigned__standard"] = 5
        data["unit__affiliate_assigned__standard"] = "days"
        resp = client.post(reverse("checklist_schedule"), data)
    assert resp.status_code == 302
    row = TaskSchedule.objects.get(kind="affiliate_assigned", variant="standard")
    assert (row.amount, row.unit) == (5, "days")
    assert "1 open task re-dated" in [str(m) for m in get_messages(resp.wsgi_request)][0]


def test_post_rejects_out_of_range(client):
    _owner(client)
    data = _defaults()
    data["amount__affiliate_assigned__standard"] = 400
    data["unit__affiliate_assigned__standard"] = "days"
    resp = client.post(reverse("checklist_schedule"), data)
    assert resp.status_code == 200
    assert b"365" in resp.content
    assert not TaskSchedule.objects.exists()


def test_reset_all_posts_defaults(client):
    _owner(client)
    TaskSchedule.objects.create(
        kind="affiliate_assigned", variant="standard", amount=5, unit="days"
    )
    resp = client.post(reverse("checklist_schedule"), _defaults())
    assert resp.status_code == 302
    assert not TaskSchedule.objects.exists()


def test_preview_uses_resolver_and_abbreviation(client, monkeypatch):
    _owner(client)
    monkeypatch.setattr(views, "_now", lambda: FIXED)
    resp = client.get(
        reverse("checklist_schedule_preview"),
        {"key": "wedding_names", "variant": "standard", "amount": 7, "unit": "days"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["text"] == "Sat Nov 14, 7:30 PM EST pickup → due Sat Nov 7, 12:00 AM EST"


def test_preview_rejects_junk(client):
    _owner(client)
    url = reverse("checklist_schedule_preview")
    base = {"key": "affiliate_assigned", "variant": "standard", "amount": 3, "unit": "days"}
    for bad in (
        {"amount": "abc"},
        {"key": "nope"},
        {"unit": "business_days"},
        {"variant": "farm_in"},
        {"amount": 999},
    ):
        resp = client.get(url, {**base, **bad})
        assert resp.status_code == 400, bad
        assert resp.json()["ok"] is False


def test_preview_formats_by_anchor():
    cases = {
        ("affiliate_assigned", "standard", 72, "hours"): (
            "Sat Nov 14, 7:30 PM EST pickup → due Wed Nov 11, 7:30 PM EST"
        ),
        ("ops_review", "standard", 24, "hours"): (
            "Opened Mon Nov 9, 9:00 AM EST → due Tue Nov 10, 9:00 AM EST"
        ),
        ("planner_change_request", "standard", 3, "business_days"): (
            "Opened Mon Nov 9, 9:00 AM EST → due Thu Nov 12, 9:00 AM EST"
        ),
        ("final_balance_paid", "farm_in", 24, "hours"): (
            "Trip ends Sat Nov 14, 7:30 PM EST → due Sun Nov 15, 7:30 PM EST"
        ),
        ("future_booking_followup", "standard", 10, "days"): (
            "Last trip Sat Nov 14, 7:30 PM EST → due Tue Nov 24, 9:00 AM EST"
        ),
        ("insurance_renewal", "standard", 2, "days"): (
            "Policy expires Sat Nov 14 → due Thu Nov 12, 9:00 AM EST"
        ),
    }
    for (key, variant, amount, unit), text in cases.items():
        assert schedule.preview(key, variant, amount, unit, now=FIXED) == text


def test_preview_value_errors():
    for args in (
        ("nope", "standard", 1, "days"),
        ("affiliate_assigned", "farm_in", 1, "days"),
        ("affiliate_assigned", "standard", 1, "business_days"),
        ("affiliate_assigned", "standard", -1, "days"),
    ):
        with pytest.raises(ValueError):
            schedule.preview(*args, now=FIXED)
