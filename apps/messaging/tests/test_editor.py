"""Storing an edited message (`messaging.editor`): what a save, reset or archive does to the
row and its queued sends, the timing rules and the worked example.

Ported from spec B's `test_overrides_save.py` and `test_overrides.py`. Spec B layered a row
over the code default ("an override"); spec C rows hold every message in full, so "not an
override" reads as "not tagged Edited" (`listing.tag`). Saves go through the editor form,
the one path that writes a row.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.messaging import defaults, editor, fields, listing, sender
from apps.messaging import rows as message_rows
from apps.messaging.factories import MessageRowFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.tests.test_switches import enabled
from apps.messaging.touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING
from apps.reservations.factories import ReservationFactory
from apps.settings.forms import MessageForm
from apps.settings.tests.test_message_editor import post_data
from apps.tasks import schedule

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)  # Mon 9:00 AM EST


def _form(kind="tp1_welcome", **over) -> MessageForm:
    """The editor form for a seeded message posting its default wording and timing (spec B's
    `save` defaults), switched on, with ``over`` applied."""
    row = MessageTemplate.objects.get(key=kind)
    default = defaults.DEFAULTS[kind]
    base = {
        "subject": default.subject,
        "email_body": default.email_body,
        "sms_body": default.sms_body,
        "timing": (default.timing_amount, default.timing_unit),
        "enabled": "on",
    }
    if over.pop("enabled", True) is False:
        over["enabled"] = None
    return MessageForm(post_data(row, **(base | over)), instance=row)


def _save(kind="tp1_welcome", **over) -> int:
    form = _form(kind, **over)
    assert form.is_valid(), form.errors
    return form.save(None)


def _edited(key: str) -> bool:
    return listing.tag(MessageTemplate.objects.get(key=key)) == "Edited"


def _none_edited() -> bool:
    return not any(listing.tag(r) == "Edited" for r in MessageTemplate.objects.live())


def test_save_stores_the_full_row():
    _save(sms_body="Hi {first_name}")
    row = MessageTemplate.objects.get(key="tp1_welcome")
    base = TEMPLATES["tp1_welcome"]
    assert (row.subject, row.email_body) == (base.subject, base.email_body)
    assert row.sms_body == "Hi {first_name}"
    assert (row.timing_amount, row.timing_unit) == (30, "minutes")


def test_save_all_default_leaves_no_override():
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Hi {first_name}")
    _save()
    assert not _edited("tp1_welcome")
    assert (
        MessageTemplate.objects.get(key="tp1_welcome").sms_body == TEMPLATES["tp1_welcome"].sms_body
    )


def test_save_keeps_row_when_only_enabled_differs():
    _save(enabled=False)
    assert MessageTemplate.objects.get(key="tp1_welcome").enabled is False


def test_save_refuses_invalid():
    count = MessageTemplate.objects.count()
    form = _form(sms_body="Hi {nope}")
    assert not form.is_valid()
    assert "sms_body" in form.errors
    # Nothing gets written (spec B started from an empty table; rows are never deleted now).
    assert MessageTemplate.objects.count() == count
    assert (
        MessageTemplate.objects.get(key="tp1_welcome").sms_body == TEMPLATES["tp1_welcome"].sms_body
    )


@pytest.mark.parametrize("timing", [(61, "days"), (2, "weeks"), (-1, "hours"), (1441, "minutes")])
def test_timing_bounds(timing):
    errors = editor.check_timing("tp2_lead_followup", "lead_created", timing)
    assert "timing" in errors
    assert "timing_amount" in _form("tp2_lead_followup", timing=timing).errors


def test_timing_equal_to_default_is_no_override():
    _save("tp2_lead_followup", timing=(2, "hours"))
    assert not _edited("tp2_lead_followup")
    row = MessageTemplate.objects.get(key="tp2_lead_followup")
    assert (row.timing_amount, row.timing_unit) == (2, "hours")


def test_untimed_trigger_refuses_timing():
    """Spec B: a message that sends when it happens has no timing. Spec C's equivalent is a
    staff click or schedule; events carry a timing."""
    errors = editor.check_timing("custom-1", "send_quote", (5, "hours"), seeded=False)
    assert errors["timing"] == "This message sends when it happens; it has no timing."


def test_untimed_trigger_ignores_stray_row_timing():
    MessageTemplate.objects.filter(key="order_confirmed").update(sms_body="Booked {quote_no}")
    assert _edited("order_confirmed")
    row = MessageRowFactory(trigger="send_quote", timing_amount=5, timing_unit="hours")
    assert message_rows.timing_of(row.key) is None


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
    MessageTemplate.objects.filter(key="tp2_lead_followup").update(
        timing_amount=5, timing_unit="hours"
    )
    t = _scheduled("tp2_lead_followup", NOW, timedelta(hours=5))
    assert editor.reset(MessageTemplate.objects.get(key="tp2_lead_followup")) == 1
    t.refresh_from_db()
    assert t.scheduled_for == NOW + timedelta(hours=2)
    assert _none_edited()


def test_reset_without_timing_change_returns_zero():
    MessageTemplate.objects.filter(key="tp2_lead_followup").update(sms_body="Hi {first_name}")
    _scheduled("tp2_lead_followup", NOW, timedelta(hours=2))
    assert editor.reset(MessageTemplate.objects.get(key="tp2_lead_followup")) == 0


def test_max_timing():
    assert editor.MAX_TIMING == {"minutes": 1440, "hours": 720, "days": 60}
    assert TouchPoint  # model import used by the redate tests


def test_timing_example_formats():
    assert (
        editor.timing_example("lead_created", 2, "hours", "after", now=NOW)
        == "Lead came in Mon Nov 9, 9:00 AM EST → sends Mon Nov 9, 11:00 AM EST"
    )
    assert (
        editor.timing_example("quote_sent", 30, "minutes", "after", now=NOW)
        == "Quote sent Mon Nov 9, 9:00 AM EST → sends Mon Nov 9, 9:30 AM EST"
    )
    assert (
        editor.timing_example("pickup_dispatched", 12, "hours", "before", now=NOW)
        == "Sat Nov 14, 7:30 PM EST pickup → sends Sat Nov 14, 7:30 AM EST"
    )
    assert (
        editor.timing_example("pickup", 72, "hours", "before", now=NOW)
        == "Sat Nov 14, 7:30 PM EST pickup → sends Wed Nov 11, 7:30 PM EST"
    )
    assert (
        editor.timing_example("quote_expiry", 24, "hours", "before", now=NOW)
        == "Quote expires Sat Nov 14, 7:30 PM EST → sends Fri Nov 13, 7:30 PM EST"
    )


def test_timing_example_rejects_bad_input():
    with pytest.raises(ValueError):
        editor.timing_example("send_quote", 1, "hours", now=NOW)
    with pytest.raises(ValueError):
        editor.timing_example("lead_created", 2, "weeks", now=NOW)
    with pytest.raises(ValueError):
        editor.timing_example("lead_created", 99999, "hours", now=NOW)


def test_schedule_helpers_are_public():
    assert schedule.stamp(schedule.sample_pickup(NOW)) == "Sat Nov 14, 7:30 PM EST"


def test_nested_format_spec_field_is_unknown():
    errors = fields.validate(
        "tp1_welcome", subject="s", email_body="{first_name:{bogus}}", sms_body="x"
    )
    assert errors["email_body"].startswith("Unknown field {bogus}")


def test_zero_timing_is_valid_and_stored():
    assert editor.check_timing("tp2_lead_followup", "lead_created", (0, "hours")) == {}
    _save("tp2_lead_followup", timing=(0, "hours"))
    assert MessageTemplate.objects.get(key="tp2_lead_followup").timing_amount == 0
    assert MessageTemplate.objects.get(key="tp2_lead_followup").offset == timedelta(0)


def test_zero_row_overrides_default_offset():
    MessageTemplate.objects.filter(key="tp2_lead_followup").update(
        timing_amount=0, timing_unit="hours"
    )
    assert MessageTemplate.objects.get(key="tp2_lead_followup").offset == timedelta(0)


def _trip(days_out=None, hours_out=None):
    when = timezone.now() + timedelta(days=days_out or 0, hours=hours_out or 0)
    local = when.astimezone(timezone.get_current_timezone())
    res = ReservationFactory(
        pickup_date=local.date(), pickup_time=local.time().replace(microsecond=0)
    )
    return res, res.pickup_at


def test_driver_released_send_now_row_redated_from_pickup():
    res, pickup = _trip(days_out=3)
    tp = TouchPointFactory(
        lead=res.lead,
        reservation=res,
        kind="driver_released",
        scheduled_for=timezone.now(),
        status="scheduled",
    )
    assert _save("driver_released", timing=(4, "hours")) == 1
    tp.refresh_from_db()
    assert tp.scheduled_for == pickup - timedelta(hours=4)


def test_driver_released_never_redated_into_the_past():
    res, pickup = _trip(hours_out=2)
    tp = TouchPointFactory(
        lead=res.lead,
        reservation=res,
        kind="driver_released",
        scheduled_for=timezone.now(),
        status="scheduled",
    )
    before = timezone.now()
    _save("driver_released", timing=(4, "hours"))
    tp.refresh_from_db()
    assert tp.scheduled_for >= before


def test_trip_confirm_customer_redated_from_pickup():
    res, pickup = _trip(days_out=10)
    tp = TouchPointFactory(
        lead=res.lead,
        reservation=res,
        kind="trip_confirm_customer",
        scheduled_for=pickup - timedelta(hours=1),
        status="scheduled",
    )
    _save("trip_confirm_customer", timing=(4, "days"))
    tp.refresh_from_db()
    assert tp.scheduled_for == pickup - timedelta(days=4)


def test_wedding_thank_you_has_its_own_timing():
    assert message_rows.timing_of("thank_you_wedding") == (2, "hours")
    assert editor.check_timing("thank_you_wedding", "all_trips_done", (1, "days")) == {}
    # Spec B's phrase "2 hours after the trips are done" is the Messages page summary now.
    row = MessageTemplate.objects.get(key="thank_you_wedding")
    assert listing.summary(row).startswith("2 hours after all trips done")
    _save("thank_you_wedding", timing=(1, "days"))
    assert message_rows.timing_of("thank_you_wedding") == (1, "days")
    assert message_rows.timing_of("thank_you") == (2, "hours")


def test_second_notice_timing_is_its_own():
    _save("trip_confirm_customer_2", timing=(36, "hours"))
    assert message_rows.timing_of("trip_confirm_customer_2") == (36, "hours")


def _check(kind, timing):
    return editor.check_timing(kind, defaults.DEFAULTS[kind].trigger, timing)


def test_second_notice_must_follow_the_first():
    first = message_rows.timing_of("trip_confirm_customer")
    assert first is not None
    assert _check("trip_confirm_customer_2", (1, "days")) == {}
    err = _check("trip_confirm_customer_2", (30, "days"))
    assert err["timing"] == "The second notice must go out after the first."
    assert _check("trip_confirm_customer_2", first)["timing"] == err["timing"]


def test_first_notice_must_precede_the_second():
    second = message_rows.timing_of("trip_confirm_customer_2")
    assert second == (2, "days")
    err = _check("trip_confirm_customer", (24, "hours"))
    assert err["timing"] == "The first notice must go out before the second."
    assert _check("trip_confirm_customer", (3, "days")) == {}


def test_save_and_reset_redate_after_the_transaction_commits(monkeypatch):
    from django.db import connection

    depth = []
    monkeypatch.setattr(
        editor,
        "_redate_checklist",
        lambda kind, changed: depth.append(len(connection.savepoint_ids)) or 0,
    )
    real = editor.events.redate
    monkeypatch.setattr(
        editor.events,
        "redate",
        lambda row, old_offset: (
            depth.append(len(connection.savepoint_ids)) or real(row, old_offset=old_offset)
        ),
    )
    _save("tp2_lead_followup", timing=(5, "hours"))
    editor.reset(MessageTemplate.objects.get(key="tp2_lead_followup"))
    assert depth == [0, 0, 0, 0]


# --- spec B over seeded rows (APC-118) -----------------------------------------------------


def test_a_freshly_seeded_table_has_no_overrides():
    assert MessageTemplate.objects.count() >= len(defaults.DEFAULTS)
    assert _none_edited()


def test_save_and_reset_never_delete_the_row():
    count = MessageTemplate.objects.count()
    _save()
    editor.reset(MessageTemplate.objects.get(key="tp1_welcome"))
    assert MessageTemplate.objects.count() == count


def test_reset_turns_a_status_message_back_off():
    MessageTemplate.objects.filter(key="status_arrived").update(
        enabled=True, sms_body="Hi {first_name}"
    )
    editor.reset(MessageTemplate.objects.get(key="status_arrived"))
    row = MessageTemplate.objects.get(key="status_arrived")
    assert row.enabled is False
    assert row.sms_body == TEMPLATES["status_arrived"].sms_body


def test_the_second_notice_row_is_untouched_by_the_first_notice():
    _save("trip_confirm_customer", enabled=False)
    assert MessageTemplate.objects.get(key="trip_confirm_customer_2").enabled is True
    editor.reset(MessageTemplate.objects.get(key="trip_confirm_customer"))
    assert MessageTemplate.objects.get(key="trip_confirm_customer").enabled is True
    assert MessageTemplate.objects.get(key="trip_confirm_customer_2").enabled is True


def test_saving_the_second_notice_changes_only_its_own_row():
    _save("trip_confirm_customer", enabled=False)
    _save("trip_confirm_customer_2", enabled=False)
    assert MessageTemplate.objects.get(key="trip_confirm_customer").enabled is False
    _save("trip_confirm_customer_2", enabled=True)
    assert MessageTemplate.objects.get(key="trip_confirm_customer").enabled is False
    assert enabled("trip_confirm_customer_2") is True


def test_the_wedding_thank_you_row_is_untouched_by_the_thank_you():
    _save("thank_you", timing=(4, "hours"), enabled=False)
    row = MessageTemplate.objects.get(key="thank_you_wedding")
    assert (row.enabled, row.timing_amount, row.timing_unit) == (True, 2, "hours")
    _save("thank_you_wedding", timing=(6, "hours"), enabled=False)
    editor.reset(MessageTemplate.objects.get(key="thank_you"))
    row.refresh_from_db()
    assert (row.enabled, row.timing_amount, row.timing_unit) == (False, 6, "hours")
    editor.reset(MessageTemplate.objects.get(key="thank_you_wedding"))
    row.refresh_from_db()
    assert (row.enabled, row.timing_amount, row.timing_unit) == (True, 2, "hours")


def test_save_keeps_the_default_timing_units_when_only_the_units_differ():
    _save("trip_confirm_customer", timing=(3, "days"))
    row = MessageTemplate.objects.get(key="trip_confirm_customer")
    assert (row.timing_amount, row.timing_unit) == (72, "hours")
    assert not _edited("trip_confirm_customer")


# --- ported from spec B's test_overrides.py ---------------------------------------------------


def _wording(row_or_template):
    """What spec B's template equality compared: wording, timing, channels and audience."""
    return (
        row_or_template.subject,
        row_or_template.email_body,
        row_or_template.sms_body,
        row_or_template.offset,
        tuple(sorted(row_or_template.channels)),
        row_or_template.audience,
    )


def test_no_row_is_code_default():
    """Spec B: no override row means the code default. Spec C: a seeded row holds it."""
    tp1 = MessageTemplate.objects.get(key="tp1_welcome")
    wedding = MessageTemplate.objects.get(key="thank_you_wedding")
    assert _wording(tp1) == _wording(TEMPLATES["tp1_welcome"])
    assert _wording(wedding) == _wording(THANK_YOU_WEDDING)


def test_row_overrides_only_non_blank():
    """Spec B layered only a row's non-blank boxes over the default. Spec C stores rows in
    full; the seeding folds a spec B row in the same way, blanks taking the default."""
    MessageTemplate.objects.filter(key="tp1_welcome").update(
        seeded=False, subject="", email_body="", sms_body="Hi {first_name}"
    )
    base = TEMPLATES["tp1_welcome"]

    defaults.seed(keys=["tp1_welcome"])
    got = MessageTemplate.objects.get(key="tp1_welcome")

    assert got.sms_body == "Hi {first_name}"
    assert got.subject == base.subject
    assert got.email_body == base.email_body
    assert got.offset == base.offset


def test_timing_row_keeps_sign():
    MessageTemplate.objects.filter(key="tp7_expiring").update(timing_amount=2, timing_unit="days")
    MessageTemplate.objects.filter(key="tp1_welcome").update(
        timing_amount=45, timing_unit="minutes"
    )

    assert MessageTemplate.objects.get(key="tp7_expiring").offset == -timedelta(days=2)
    assert MessageTemplate.objects.get(key="tp1_welcome").offset == timedelta(minutes=45)


def test_timing_of_defaults():
    assert message_rows.timing_of("tp1_welcome") == (30, "minutes")
    assert message_rows.timing_of("tp2_lead_followup") == (2, "hours")
    assert message_rows.timing_of("tp6_quote_followup") == (1, "days")
    assert message_rows.timing_of("payment_reminder") == (3, "days")
    assert message_rows.timing_of("driver_released") == (12, "hours")
    # Spec B: order_confirmed sends when it happens. Spec C's untimed rows are staff clicks.
    assert message_rows.timing_of(MessageRowFactory(trigger="send_quote").key) is None


def test_timing_of_uses_row():
    MessageTemplate.objects.filter(key="tp1_welcome").update(timing_amount=2, timing_unit="hours")
    assert message_rows.timing_of("tp1_welcome") == (2, "hours")


def _thank_you_row(lead) -> MessageTemplate:
    """The row a queued thank-you sends for this order (the old engine's
    `_thank_you_template`)."""
    return sender._row_for(TouchPoint(lead=lead, kind="thank_you"), None)


def test_wedding_thank_you_is_its_own_row():
    MessageTemplate.objects.filter(key="thank_you_wedding").update(email_body="WEDDING BODY")
    wedding = LeadFactory(wedding_name="Smith")
    plain = LeadFactory(wedding_name="")

    assert _thank_you_row(wedding).email_body == "WEDDING BODY"
    assert _wording(_thank_you_row(plain)) == _wording(TEMPLATES["thank_you"])


def test_the_sample_preview_fills_the_day_list_with_drivers():
    """APC-131: previewing without an order shows a driver under the sample trip."""
    row = MessageRowFactory(
        trigger="pickup_dispatched",
        section=MessageTemplate.Section.TRIP_DAY,
        send_email=False,
        sms_body="Your drivers:\n{trip_sheet_drivers}",
        recipients=["booking_contact"],
    )
    sms = editor.preview(row, None)["sms"]
    assert "Driver: Sam Driver" in sms and "{trip_sheet_drivers}" not in sms
