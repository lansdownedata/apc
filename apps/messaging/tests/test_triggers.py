from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from apps.messaging import fields, triggers
from apps.messaging.defaults import DEFAULTS

SPEC_KEYS = {
    # Dates
    "pickup",
    "pickup_dispatched",
    "quote_expiry",
    "balance_due",
    # Events, order level
    "lead_created",
    "wedding_details_received",
    "quote_sent",
    "quote_viewed",
    "order_confirmed",
    "order_cancelled",
    "deposit_hold_released",
    "deposit_paid",
    "balance_paid",
    "all_trips_done",
    # Events, trip level
    "affiliate_accepted",
    "status_dispatched",
    "status_on_the_way",
    "status_arrived",
    "driver_changed",
    "trip_cancelled",
    "trip_done",
    # Staff clicks
    "send_quote",
    "send_pay_link",
    "send_terms_link",
    "payment_receipt",
    "affiliate_offer",
    "affiliate_cancel",
    "planner_invite",
    "planner_password_reset",
    "user_invite",
    # Schedules
    "deposit_report",
    "review_report",
    "complaint_report",
    "unconfirmed_trips_report",
    "task_digest",
    "dispatch_alert",
}

DATES = {"pickup", "pickup_dispatched", "quote_expiry", "balance_due"}
TRIP_LEVEL = {
    "pickup",
    "pickup_dispatched",
    "affiliate_accepted",
    "status_dispatched",
    "status_on_the_way",
    "status_arrived",
    "driver_changed",
    "trip_cancelled",
    "trip_done",
    "affiliate_offer",
    "affiliate_cancel",
}
DRIVER = {
    "pickup_dispatched",
    "status_dispatched",
    "status_on_the_way",
    "status_arrived",
    "driver_changed",
    "trip_done",
}


def test_catalog_keys_are_the_spec_list():
    assert set(triggers.CATALOG) == SPEC_KEYS
    for key, trigger in triggers.CATALOG.items():
        assert trigger.key == key


def test_group_labels():
    assert triggers.GROUP_LABEL == {
        "date": "Dates",
        "event": "Events",
        "staff_click": "Staff clicks",
        "schedule": "Schedules",
    }


def test_get():
    assert triggers.get("pickup") is triggers.CATALOG["pickup"]
    with pytest.raises(KeyError):
        triggers.get("nope")


def test_kinds_and_levels():
    for key, trigger in triggers.CATALOG.items():
        assert trigger.kind in triggers.GROUP_LABEL
        assert (trigger.kind == "date") is (key in DATES)
        assert trigger.level == ("trip" if key in TRIP_LEVEL else "order"), key
        assert (trigger.anchor is not None) is (key in DATES), key


def test_field_groups_are_real_groups():
    for trigger in triggers.CATALOG.values():
        assert trigger.field_groups <= set(fields._GROUPS), trigger.key
        assert trigger.audiences <= {"customer", "affiliate", "planner", "staff"}
        assert trigger.audiences, trigger.key


def test_driver_group_only_where_a_driver_exists():
    have = {k for k, t in triggers.CATALOG.items() if "Driver" in t.field_groups}
    assert have == DRIVER


def test_trip_group_only_on_trip_level_triggers():
    for key, trigger in triggers.CATALOG.items():
        if "Trip" in trigger.field_groups:
            assert trigger.level == "trip", key
        if "Affiliate" in trigger.field_groups:
            assert "affiliate" in trigger.audiences, key


def test_affiliate_accepts_cannot_target_a_planner():
    assert "planner" not in triggers.get("affiliate_accepted").audiences


def test_combine_waits_for_and_email_via():
    assert {k for k, t in triggers.CATALOG.items() if t.combine} == {"pickup", "pickup_dispatched"}
    assert {k for k, t in triggers.CATALOG.items() if t.waits_for} == {"pickup_dispatched"}
    assert triggers.get("pickup_dispatched").waits_for == "dispatched_with_driver"
    postmark = {k for k, t in triggers.CATALOG.items() if t.email_via == "postmark"}
    assert postmark == {
        k for k, t in triggers.CATALOG.items() if t.kind in ("staff_click", "schedule")
    } | {"wedding_details_received"}


def test_every_default_trigger_is_in_the_catalog():
    for key, default in DEFAULTS.items():
        assert default.trigger in triggers.CATALOG, key
        assert default.audience in triggers.get(default.trigger).audiences, key


def test_every_default_placeholder_is_one_its_trigger_can_fill():
    for key, default in DEFAULTS.items():
        groups = triggers.get(default.trigger).field_groups
        allowed = {name for group in groups for name, _ in fields._GROUPS[group]}
        used = set()
        for text in (default.subject, default.email_body, default.sms_body):
            used |= fields._names(text)
        assert used <= allowed, (key, sorted(used - allowed))


def test_pickup_anchors_read_the_trip():
    at = datetime(2027, 4, 20, 9, 30, tzinfo=ZoneInfo("America/Chicago"))
    trip = SimpleNamespace(pickup_at=at)
    assert triggers.get("pickup").anchor(None, trip) == at
    assert triggers.get("pickup_dispatched").anchor(None, trip) == at


def test_quote_expiry_anchor():
    at = datetime(2027, 4, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
    lead = SimpleNamespace(quote_expires_at=at)
    assert triggers.get("quote_expiry").anchor(lead, None) == at


def test_balance_due_anchor_is_midnight_company_time(settings):
    settings.TIME_ZONE = "America/New_York"
    lead = SimpleNamespace(payment=SimpleNamespace(balance_due_date=date(2027, 5, 14)))
    got = triggers.get("balance_due").anchor(lead, None)
    assert got == datetime.combine(date(2027, 5, 14), time.min, ZoneInfo("America/New_York"))


def test_balance_due_anchor_without_a_plan_or_date():
    assert triggers.get("balance_due").anchor(SimpleNamespace(), None) is None
    lead = SimpleNamespace(payment=SimpleNamespace(balance_due_date=None))
    assert triggers.get("balance_due").anchor(lead, None) is None
