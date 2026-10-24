"""APC-49 — the TaskConfig singleton and its settings screen."""

from decimal import Decimal

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department, User
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db


def test_load_is_idempotent():
    first = TaskConfig.load()
    second = TaskConfig.load()

    assert first.pk == second.pk == 1
    assert TaskConfig.objects.count() == 1
    assert first.enabled is True
    assert first.overdue_grace_hours == 24


@override_settings(COMPANY_EMAIL="office@allprocharter.com")
def test_digest_list_falls_back_to_company_email():
    cfg = TaskConfig.load()

    assert cfg.digest_list == ["office@allprocharter.com"]

    cfg.digest_emails = "a@allprocharter.com,\nb@allprocharter.com"
    assert cfg.digest_list == ["a@allprocharter.com", "b@allprocharter.com"]


def test_owner_for_reads_the_department_owner():
    owner = UserFactory()
    cfg = TaskConfig.load()
    cfg.accounting_owner = owner
    cfg.save()

    assert cfg.owner_for(Department.ACCOUNTING) == owner
    assert cfg.owner_for(Department.SALES) is None


def test_screen_requires_owner_admin(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    assert client.get(reverse("task_settings")).status_code == 403


def _rule(cfg):
    return (
        cfg.grace_large_minutes,
        cfg.grace_standard_minutes,
        cfg.transfer_grace_minutes,
        cfg.airport_domestic_grace_minutes,
        cfg.airport_international_grace_minutes,
        cfg.overtime_block_minutes,
    )


def test_the_overtime_rule_defaults_to_the_clients_numbers():
    """Client, 2026-10-19: 5-min grace for bus/van/stretch, 15 for sedan/SUV, 15 on any
    transfer, 25 after a domestic arrival, 50 after an international one; then whole hours."""
    assert _rule(TaskConfig.load()) == (5, 15, 15, 25, 50, 60)


def test_the_cleaning_fee_defaults_to_zero():
    """APC-94: the client's number is entered in Settings when he sends it."""
    assert TaskConfig.load().cleaning_fee == Decimal("0.00")


def test_the_overtime_block_cannot_be_zero(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    response = client.post(
        reverse("task_settings"),
        {
            "enabled": "on",
            "overdue_grace_hours": 12,
            "post_trip_grace_hours": 3,
            "grace_large_minutes": 5,
            "grace_standard_minutes": 15,
            "transfer_grace_minutes": 15,
            "airport_domestic_grace_minutes": 25,
            "airport_international_grace_minutes": 50,
            "overtime_block_minutes": 0,
            "future_booking_offset_days": 330,
        },
    )

    assert response.status_code == 200
    assert TaskConfig.load().overtime_block_minutes == 60


def test_owner_admin_can_save(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    ops = UserFactory()

    resp = client.post(
        reverse("task_settings"),
        {
            "enabled": "on",
            "operations_owner": ops.pk,
            "overdue_grace_hours": 12,
            "post_trip_grace_hours": 3,
            "grace_large_minutes": 4,
            "grace_standard_minutes": 10,
            "transfer_grace_minutes": 12,
            "airport_domestic_grace_minutes": 20,
            "airport_international_grace_minutes": 45,
            "overtime_block_minutes": 30,
            "cleaning_fee": "250.00",
            "future_booking_offset_days": 360,
            "digest_emails": "ops@allprocharter.com",
        },
    )

    assert resp.status_code == 302
    cfg = TaskConfig.load()
    assert cfg.operations_owner == ops
    assert cfg.overdue_grace_hours == 12
    assert cfg.post_trip_grace_hours == 3
    assert _rule(cfg) == (4, 10, 12, 20, 45, 30)
    assert cfg.cleaning_fee == Decimal("250.00")
    assert cfg.future_booking_offset_days == 360
    assert cfg.sales_owner is None
    assert TaskConfig.objects.count() == 1


def test_owner_pickers_are_searchable_selects_not_native(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("task_settings")).content.decode()

    assert 'name="operations_owner"' in body
    for chunk in body.split("<select")[1:]:
        assert "data-tom" in chunk.split(">")[0]


def test_the_settings_index_links_to_it(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("settings_index")).content.decode()

    assert reverse("task_settings") in body
