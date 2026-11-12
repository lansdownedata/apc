"""APC-49 — the TaskConfig singleton and its settings screen."""

import importlib
from datetime import time
from decimal import Decimal

import pytest
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


def test_digest_list_falls_back_to_company_email():
    """The digest list now lives on its message row; migration 0028 filled it this way."""
    seed = importlib.import_module("apps.messaging.migrations.0028_seed_report_rows")

    def digest_list(digest_emails):
        return seed.staff_lists(
            {},
            digest_emails=digest_emails,
            report_emails="",
            alert_emails="",
            company_email="office@allprocharter.com",
        )["task_digest"]

    assert digest_list("") == ["office@allprocharter.com"]

    assert digest_list("a@allprocharter.com,\nb@allprocharter.com") == [
        "a@allprocharter.com",
        "b@allprocharter.com",
    ]


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


def test_hold_switch_ships_off():
    """APC-95: holds are dark until someone turns them on in Settings."""
    cfg = TaskConfig.load()
    assert cfg.hold_enabled is False
    assert cfg.hold_place_time == time(3, 0)
    assert cfg.hold_release_days == 3
    assert cfg.hold_lead_hours == 6
    assert not hasattr(cfg, "cleaning_fee")  # the fee moved to each vehicle type


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
            "hold_place_time": "03:00",
            "hold_lead_hours": 6,
            "hold_release_days": 3,
        },
    )

    assert resp.status_code == 302
    cfg = TaskConfig.load()
    assert cfg.operations_owner == ops
    assert cfg.overdue_grace_hours == 12
    assert cfg.post_trip_grace_hours == 3
    assert _rule(cfg) == (4, 10, 12, 20, 45, 30)
    assert cfg.sales_owner is None
    assert TaskConfig.objects.count() == 1


def test_owner_pickers_are_searchable_selects_not_native(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("task_settings")).content.decode()

    assert 'name="operations_owner"' in body
    for chunk in body.split("<select")[1:]:
        assert "data-tom" in chunk.split(">")[0]


def test_the_settings_menu_links_to_it(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("settings_index")).content.decode()

    assert reverse("task_settings") in body


def test_hold_settings_are_labelled_fields_not_a_sentence_around_inputs(client):
    """SET-11: the hold sentence wrapped around its inputs ("Place the hold at [03:00 AM]"
    then the rest on its own line). Each setting is a labelled field of its own; since the
    2026-11-11 redesign, one per row like every other setting on the screen."""
    import re

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    body = client.get(reverse("task_settings")).content.decode()

    block = body[body.index("Cleaning hold") : body.index("Reports")]
    for name in ("hold_place_time", "hold_lead_hours", "hold_release_days"):
        label = re.search(rf'<label for="id_{name}"[^>]*>([^<]*)</label>', block)
        assert label, name
        assert label.group(1).strip()


def test_tasks_screen_saves_hold_settings(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("task_settings")).content.decode()
    assert "Cleaning hold" in body
    assert "Place cleaning holds" in body
    assert "On the day of the order&#x27;s first held trip, in the trip&#x27;s local time." in body
    assert "After the order&#x27;s last held trip, at the latest." in body
    assert "Whichever comes first." in body
    assert 'type="time"' in body
    assert "cleaning_fee" not in body  # the fee is set per vehicle type now

    resp = client.post(
        reverse("task_settings"),
        {
            "enabled": "on",
            "overdue_grace_hours": 24,
            "post_trip_grace_hours": 2,
            "grace_large_minutes": 5,
            "grace_standard_minutes": 15,
            "transfer_grace_minutes": 15,
            "airport_domestic_grace_minutes": 25,
            "airport_international_grace_minutes": 50,
            "overtime_block_minutes": 60,
            "hold_enabled": "on",
            "hold_place_time": "04:30",
            "hold_lead_hours": 8,
            "hold_release_days": 5,
        },
    )

    assert resp.status_code == 302
    cfg = TaskConfig.load()
    assert cfg.hold_enabled is True
    assert cfg.hold_place_time == time(4, 30)
    assert cfg.hold_lead_hours == 8
    assert cfg.hold_release_days == 5


@pytest.mark.parametrize("hours", ["-1", "25", ""])
def test_the_hold_lead_hours_must_be_0_to_24(client, hours):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    resp = client.post(
        reverse("task_settings"),
        {
            "enabled": "on",
            "overdue_grace_hours": 24,
            "post_trip_grace_hours": 2,
            "grace_large_minutes": 5,
            "grace_standard_minutes": 15,
            "transfer_grace_minutes": 15,
            "airport_domestic_grace_minutes": 25,
            "airport_international_grace_minutes": 50,
            "overtime_block_minutes": 60,
            "hold_place_time": "03:00",
            "hold_lead_hours": hours,
            "hold_release_days": 3,
        },
    )

    assert resp.status_code == 200
    assert TaskConfig.load().hold_lead_hours == 6


def test_zero_lead_hours_is_allowed(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    resp = client.post(
        reverse("task_settings"),
        {
            "enabled": "on",
            "overdue_grace_hours": 24,
            "post_trip_grace_hours": 2,
            "grace_large_minutes": 5,
            "grace_standard_minutes": 15,
            "transfer_grace_minutes": 15,
            "airport_domestic_grace_minutes": 25,
            "airport_international_grace_minutes": 50,
            "overtime_block_minutes": 60,
            "hold_place_time": "03:00",
            "hold_lead_hours": 0,
            "hold_release_days": 3,
        },
    )

    assert resp.status_code == 302
    assert TaskConfig.load().hold_lead_hours == 0


# --- the fee moves onto the vehicle types (APC-95) ----------------------------------------


def _copy_fee_migration():
    return importlib.import_module("apps.tasks.migrations.0017_hold_settings")


@pytest.mark.parametrize(("old_fee", "large_gets"), [("250.00", "250.00"), ("0.00", "0.00")])
def test_migration_copies_global_fee_to_large_types_only_when_positive(old_fee, large_gets):
    """The data step runs against the live models; only reading the old column is stubbed,
    because the test database is already past the migration that drops it."""
    from unittest.mock import patch

    from django.apps import apps

    from apps.leads.factories import VehicleTypeFactory
    from apps.leads.models import VehicleType

    bus = VehicleTypeFactory(name="Mini Bus", overtime_class=VehicleType.OvertimeClass.LARGE)
    coach = VehicleTypeFactory(
        name="Coach", overtime_class=VehicleType.OvertimeClass.LARGE, cleaning_fee=Decimal("90")
    )
    sedan = VehicleTypeFactory(name="Sedan", overtime_class=VehicleType.OvertimeClass.STANDARD)
    migration = _copy_fee_migration()

    with patch.object(migration, "_old_fee", return_value=Decimal(old_fee)):
        migration.copy_fee(apps, None)

    for row in (bus, coach, sedan):
        row.refresh_from_db()
    assert bus.cleaning_fee == Decimal(large_gets)
    # A zero global fee copies nothing: an already-set type keeps its own number.
    assert coach.cleaning_fee == (Decimal(large_gets) if old_fee != "0.00" else Decimal("90"))
    assert sedan.cleaning_fee == Decimal("0.00")


def test_the_migration_reads_the_old_fee_before_dropping_the_column():
    from django.db import migrations

    ops = _copy_fee_migration().Migration.operations
    kinds = [type(op) for op in ops]
    copy_at = kinds.index(migrations.RunPython)
    remove_at = next(i for i, op in enumerate(ops) if isinstance(op, migrations.RemoveField))
    assert copy_at < remove_at
    assert ops[remove_at].name == "cleaning_fee"


_POST = {
    "enabled": "on",
    "overdue_grace_hours": 24,
    "post_trip_grace_hours": 2,
    "grace_large_minutes": 5,
    "grace_standard_minutes": 15,
    "transfer_grace_minutes": 15,
    "airport_domestic_grace_minutes": 25,
    "airport_international_grace_minutes": 50,
    "overtime_block_minutes": 60,
    "hold_place_time": "03:00",
    "hold_lead_hours": 6,
    "hold_release_days": 3,
}


def test_the_hold_service_types_are_picked_on_the_tasks_screen(client):
    """Which service types get a cleaning hold is chosen here and stored on each type."""
    from apps.leads.factories import ServiceTypeFactory

    wedding = ServiceTypeFactory(name="Hold Wedding")
    airport = ServiceTypeFactory(name="Hold Airport")
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    body = client.get(reverse("task_settings")).content.decode()
    picker = body[body.index('name="hold_service_types"') - 200 :]
    assert "data-tom" in picker and "multiple" in picker
    assert f'<option value="{wedding.pk}" selected' in body  # every type is held by default

    resp = client.post(reverse("task_settings"), _POST | {"hold_service_types": [wedding.pk]})

    assert resp.status_code == 302
    wedding.refresh_from_db()
    airport.refresh_from_db()
    assert wedding.cleaning_hold is True
    assert airport.cleaning_hold is False


def test_every_setting_on_the_tasks_screen_is_a_labelled_row(client):
    """One pattern for the whole screen: label and note on the left, control on the right."""
    import re

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    body = client.get(reverse("task_settings")).content.decode()

    rows = re.findall(r'data-setting-row="([\w]+)"', body)
    assert rows == [
        "enabled",
        "sales_owner",
        "operations_owner",
        "affiliate_mgmt_owner",
        "customer_service_owner",
        "accounting_owner",
        "overdue_grace_hours",
        "post_trip_grace_hours",
        "grace_large_minutes",
        "grace_standard_minutes",
        "transfer_grace_minutes",
        "airport_domestic_grace_minutes",
        "airport_international_grace_minutes",
        "overtime_block_minutes",
        "hold_enabled",
        "hold_service_types",
        "hold_place_time",
        "hold_lead_hours",
        "hold_release_days",
    ]
    for name in rows:
        assert re.search(rf'<label for="id_{name}"[^>]*>\s*\S', body), name
