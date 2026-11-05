import importlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.messaging import events, rules, sender
from apps.messaging.models import MessageTemplate, NotificationConfig, TouchPoint

pytestmark = pytest.mark.django_db


def enabled(key: str) -> bool:
    """Whether a message may go out at all: the sender's first gate (`rules.check`), the
    master switch and then the row's own switch. Spec B asked `overrides.is_enabled`."""
    row = MessageTemplate.objects.get(key=key)
    tp = TouchPoint(lead=LeadFactory(), kind=key, message=row)
    return rules.check(tp, row) != ("skip", rules.DISABLED)


def test_defaults_match_today():
    for kind in ("tp1_welcome", "driver_released", "order_confirmed", "thank_you", "tp8_expired"):
        assert enabled(kind) is True
    for kind in ("status_on_the_way", "status_arrived", "status_dispatched"):
        assert enabled(kind) is False


def test_row_switch_wins():
    MessageTemplate.objects.filter(key="status_arrived").update(enabled=True)
    MessageTemplate.objects.filter(key="tp1_welcome").update(enabled=False)
    assert enabled("status_arrived") is True
    assert enabled("tp1_welcome") is False


def test_master_switch_off_disables_all():
    NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})
    MessageTemplate.objects.filter(key="status_arrived").update(enabled=True)
    for kind in ("tp1_welcome", "driver_released", "status_arrived", "thank_you"):
        assert enabled(kind) is False


def test_second_wave_has_its_own_switch():
    MessageTemplate.objects.filter(key="trip_confirm_customer").update(enabled=False)
    assert enabled("trip_confirm_customer_2") is True
    MessageTemplate.objects.filter(key="trip_confirm_customer").update(enabled=True)
    MessageTemplate.objects.filter(key="trip_confirm_customer_2").update(enabled=False)
    assert enabled("trip_confirm_customer") is True
    assert enabled("trip_confirm_customer_2") is False


def test_wedding_thank_you_has_its_own_switch():
    MessageTemplate.objects.filter(key="thank_you").update(enabled=False)
    assert enabled("thank_you_wedding") is True
    MessageTemplate.objects.filter(key="thank_you").update(enabled=True)
    MessageTemplate.objects.filter(key="thank_you_wedding").update(enabled=False)
    assert enabled("thank_you") is True
    assert enabled("thank_you_wedding") is False


def test_disabled_kind_skips_at_send(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory()
    tp = TouchPoint.objects.create(
        lead=lead,
        kind=TouchPoint.Kind.TP1_WELCOME,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    MessageTemplate.objects.filter(key="tp1_welcome").update(enabled=False)

    sender.run_touchpoints()

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    assert tp.error == "message disabled"


def test_tp_program_now_switchable():
    MessageTemplate.objects.filter(key="tp1_welcome").update(enabled=False)
    lead = LeadFactory()

    events.emit("lead_created", lead)

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
