import importlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.messaging import overrides, touchpoints
from apps.messaging.models import MessageTemplate, NotificationConfig, TouchPoint

pytestmark = pytest.mark.django_db


def test_defaults_match_today():
    for kind in ("tp1_welcome", "driver_released", "order_confirmed", "thank_you", "tp8_expired"):
        assert overrides.is_enabled(kind) is True
    for kind in ("status_on_the_way", "status_arrived", "status_dispatched"):
        assert overrides.is_enabled(kind) is False


def test_row_switch_wins():
    MessageTemplate.objects.create(kind="status_arrived", enabled=True)
    MessageTemplate.objects.create(kind="tp1_welcome", enabled=False)
    assert overrides.is_enabled("status_arrived") is True
    assert overrides.is_enabled("tp1_welcome") is False


def test_master_switch_off_disables_all():
    NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})
    MessageTemplate.objects.create(kind="status_arrived", enabled=True)
    for kind in ("tp1_welcome", "driver_released", "status_arrived", "thank_you"):
        assert overrides.is_enabled(kind) is False


def test_second_wave_follows_first():
    MessageTemplate.objects.create(kind="trip_confirm_customer", enabled=False)
    assert overrides.is_enabled("trip_confirm_customer_2") is False


def test_wedding_thank_you_shares_the_thank_you_switch():
    MessageTemplate.objects.create(kind="thank_you", enabled=False)
    assert overrides.is_enabled("thank_you_wedding") is False


def test_disabled_kind_skips_at_send(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory()
    tp = TouchPoint.objects.create(
        lead=lead,
        kind=TouchPoint.Kind.TP1_WELCOME,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    MessageTemplate.objects.create(kind="tp1_welcome", enabled=False)

    touchpoints.run_touchpoints()

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    assert tp.error == "message disabled"


def test_tp_program_now_switchable():
    MessageTemplate.objects.create(kind="tp1_welcome", enabled=False)
    lead = LeadFactory()

    touchpoints.schedule_lead_created(lead)

    kinds = set(TouchPoint.objects.filter(lead=lead).values_list("kind", flat=True))
    assert "tp1_welcome" not in kinds
    assert "tp2_lead_followup" in kinds


def test_migration_copies_only_differences():
    mod = importlib.import_module(
        "apps.messaging.migrations.0017_copy_switches_to_message_templates"
    )
    flags = {
        "wed_final_details": True,
        "order_confirmed": False,
        "status_arrived": True,
        "status_dispatched": False,
    }
    assert mod.rows_for(flags) == {"order_confirmed": False, "status_arrived": True}
