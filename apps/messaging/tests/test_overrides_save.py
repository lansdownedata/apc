from datetime import UTC, datetime, timedelta

import pytest

from apps.leads.factories import LeadFactory
from apps.messaging import fields, overrides
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.touchpoint_templates import TEMPLATES
from apps.tasks import schedule

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)  # Mon 9:00 AM EST


def _save(kind="tp1_welcome", **over):
    base = TEMPLATES[kind]
    args = dict(
        subject=base.subject,
        email_body=base.email_body,
        sms_body=base.sms_body,
        timing=None,
        enabled=True,
        user=None,
    )
    args.update(over)
    return overrides.save(kind, **args)


def test_save_stores_only_differences():
    _save(sms_body="Hi {first_name}")
    row = MessageTemplate.objects.get(kind="tp1_welcome")
    assert row.subject == "" and row.email_body == ""
    assert row.sms_body == "Hi {first_name}"
    assert row.timing_amount is None


def test_save_all_default_deletes_row():
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hi {first_name}")
    _save()
    assert not MessageTemplate.objects.filter(kind="tp1_welcome").exists()


def test_save_keeps_row_when_only_enabled_differs():
    _save(enabled=False)
    assert MessageTemplate.objects.get(kind="tp1_welcome").enabled is False


def test_save_refuses_invalid():
    with pytest.raises(overrides.SaveError) as exc:
        _save(sms_body="Hi {nope}")
    assert "sms_body" in exc.value.errors
    assert not MessageTemplate.objects.exists()


@pytest.mark.parametrize("timing", [(61, "days"), (2, "weeks"), (0, "hours"), (1441, "minutes")])
def test_timing_bounds(timing):
    with pytest.raises(overrides.SaveError) as exc:
        _save("tp2_lead_followup", timing=timing)
    assert "timing" in exc.value.errors


def test_timing_equal_to_default_stored_null():
    _save("tp2_lead_followup", timing=(2, "hours"))
    assert not MessageTemplate.objects.filter(kind="tp2_lead_followup").exists()


def test_triggered_kind_refuses_timing():
    errors = overrides.check(
        "order_confirmed", subject="s", email_body="b", sms_body="m", timing=(5, "hours")
    )
    assert errors["timing"] == "This message sends when it happens; it has no timing."


def test_triggered_kind_ignores_stray_row_timing():
    MessageTemplate.objects.create(kind="order_confirmed", timing_amount=5, timing_unit="hours")
    assert overrides.effective("order_confirmed").offset == timedelta(0)


def _scheduled(kind, anchor, offset):
    return TouchPointFactory(
        lead=LeadFactory(), kind=kind, scheduled_for=anchor + offset, status="scheduled"
    )


def test_timing_change_redates_scheduled_only():
    a1, a2 = NOW, NOW + timedelta(days=1)
    t1 = _scheduled("tp2_lead_followup", a1, timedelta(hours=2))
    t2 = _scheduled("tp2_lead_followup", a2, timedelta(hours=2))
    sent = TouchPointFactory(
        lead=LeadFactory(),
        kind="tp2_lead_followup",
        status="sent",
        scheduled_for=a1 + timedelta(hours=2),
    )
    assert _save("tp2_lead_followup", timing=(5, "hours")) == 2
    t1.refresh_from_db(), t2.refresh_from_db(), sent.refresh_from_db()
    assert t1.scheduled_for == a1 + timedelta(hours=5)
    assert t2.scheduled_for == a2 + timedelta(hours=5)
    assert sent.scheduled_for == a1 + timedelta(hours=2)


def test_negative_offset_kind_redates_with_sign():
    t = _scheduled("tp7_expiring", NOW, timedelta(hours=-24))
    assert _save("tp7_expiring", timing=(2, "days")) == 1
    t.refresh_from_db()
    assert t.scheduled_for == NOW - timedelta(days=2)


def test_text_change_redates_nothing():
    _scheduled("tp2_lead_followup", NOW, timedelta(hours=2))
    assert _save("tp2_lead_followup", email_body="Hello {first_name}") == 0


def test_reset_redates():
    MessageTemplate.objects.create(kind="tp2_lead_followup", timing_amount=5, timing_unit="hours")
    t = _scheduled("tp2_lead_followup", NOW, timedelta(hours=5))
    assert overrides.reset("tp2_lead_followup") == 1
    t.refresh_from_db()
    assert t.scheduled_for == NOW + timedelta(hours=2)
    assert not MessageTemplate.objects.exists()


def test_reset_without_timing_change_returns_zero():
    MessageTemplate.objects.create(kind="tp2_lead_followup", sms_body="Hi {first_name}")
    _scheduled("tp2_lead_followup", NOW, timedelta(hours=2))
    assert overrides.reset("tp2_lead_followup") == 0


def test_max_timing():
    assert overrides.MAX_TIMING == {"minutes": 1440, "hours": 720, "days": 60}
    assert TouchPoint  # model import used by the redate tests


def test_timing_example_formats():
    assert (
        overrides.timing_example("tp2_lead_followup", 2, "hours", now=NOW)
        == "Lead came in Mon Nov 9, 9:00 AM EST → sends Mon Nov 9, 11:00 AM EST"
    )
    assert (
        overrides.timing_example("tp3_quote_sent_sms", 30, "minutes", now=NOW)
        == "Quote sent Mon Nov 9, 9:00 AM EST → sends Mon Nov 9, 9:30 AM EST"
    )
    assert (
        overrides.timing_example("driver_released", 12, "hours", now=NOW)
        == "Sat Nov 14, 7:30 PM EST pickup → sends Sat Nov 14, 7:30 AM EST"
    )
    assert (
        overrides.timing_example("trip_confirm_customer", 72, "hours", now=NOW)
        == "Sat Nov 14, 7:30 PM EST pickup → sends Wed Nov 11, 7:30 PM EST"
    )
    assert (
        overrides.timing_example("tp7_expiring", 24, "hours", now=NOW)
        == "Quote expires Sat Nov 14, 7:30 PM EST → sends Fri Nov 13, 7:30 PM EST"
    )


def test_timing_example_rejects_bad_input():
    with pytest.raises(ValueError):
        overrides.timing_example("order_confirmed", 1, "hours", now=NOW)
    with pytest.raises(ValueError):
        overrides.timing_example("tp2_lead_followup", 2, "weeks", now=NOW)
    with pytest.raises(ValueError):
        overrides.timing_example("tp2_lead_followup", 99999, "hours", now=NOW)


def test_schedule_helpers_are_public():
    assert schedule.stamp(schedule.sample_pickup(NOW)) == "Sat Nov 14, 7:30 PM EST"


def test_nested_format_spec_field_is_unknown():
    errors = fields.validate(
        "tp1_welcome", subject="s", email_body="{first_name:{bogus}}", sms_body="x"
    )
    assert errors["email_body"].startswith("Unknown field {bogus}")
