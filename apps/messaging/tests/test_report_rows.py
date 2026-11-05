"""The staff reports as message rows (spec C sections 3, 7 and 8, APC-120).

Each report job sends its seeded row through `sender.send_now`: the wording around the
table comes from the row, the table from the job as one block, and the recipients are the
row's staff emails, which Admins edit on the row (they used to live in two env settings and
on the Tasks and Dispatch alerts screens). Schedules and "skip if empty" stay the job's. What
each report sends word for word is held by `test_reports_golden.py`; this file holds the
rows, their locks, their recipients and their switch.
"""

import importlib
import re
from unittest.mock import patch

import pytest
from django.conf import settings as django_settings
from django.core import mail
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.dispatch.models import DispatchAlertConfig
from apps.leads.factories import ServiceTypeFactory
from apps.messaging import defaults, editor, fields, listing, triggers
from apps.messaging.models import Delivery, MessageTemplate, NotificationConfig, TouchPoint
from apps.settings.tests.test_message_editor import post_data
from apps.tasks.models import TaskConfig

from .golden import scenarios as s
from .test_reports_golden import REPORTS as SCENARIOS

pytestmark = pytest.mark.django_db

KEYS = (
    "deposit_report",
    "review_report",
    "complaint_report",
    "unconfirmed_trips_report",
    "task_digest",
    "dispatch_alert",
)
# A scenario with one thing in it, and one with nothing, per report.
ONE = {
    "deposit_report": "deposit_report_one_order",
    "unconfirmed_trips_report": "unconfirmed_one_trip",
    "review_report": "review_report_no_ratings",
    "complaint_report": "complaint_report_one",
    "task_digest": "task_digest_one_task",
    "dispatch_alert": "dispatch_alert_warning_only",
}
NONE = {
    "deposit_report": "deposit_report_empty",
    "unconfirmed_trips_report": "unconfirmed_empty",
    "review_report": "review_report_empty",
    "complaint_report": "complaint_report_empty",
    "task_digest": "task_digest_nothing_overdue",
    "dispatch_alert": "dispatch_alert_nothing",
}
SEND = "apps.integrations.podium.send_message"
SEED = "apps.messaging.migrations.0028_seed_report_rows"


@pytest.fixture(autouse=True)
def live(settings):
    settings.TOUCHPOINTS_ENABLED = False
    settings.OUTBOUND_ENABLED = False
    settings.PUBLIC_BASE_URL = s.BASE_URL
    settings.TIME_ZONE = "America/New_York"
    settings.COMPANY_PHONE = "(703) 555-0100"
    settings.BALANCE_CHARGE_DAYS_BEFORE = 30


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _staff(key: str, emails: list[str]) -> None:
    MessageTemplate.objects.filter(key=key).update(staff_emails=emails)


def _run(scenario: str):
    with s.frozen(), patch(SEND, return_value={"uid": "m"}) as texts:
        SCENARIOS[scenario]()
    return texts


def _owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


def _edit(client, key, **over):
    return client.post(reverse("message_edit", args=[key]), post_data(_row(key), **over))


# --- the seeded rows -------------------------------------------------------------------


def test_the_report_originals_sit_on_their_schedule_triggers():
    assert set(defaults.REPORTS) == set(KEYS)
    assert not set(defaults.REPORTS) & (
        set(defaults.DEFAULTS) | set(defaults.CLICKS) | set(defaults.INVITES)
    )
    for key in KEYS:
        d = defaults.ORIGINALS[key]
        assert (d.trigger, d.audience, d.section) == (key, "staff", "staff_reports"), key
        assert triggers.get(d.trigger).kind == "schedule"
        assert (d.send_email, d.send_sms) == (True, False)
        assert "{report_table}" in d.email_body.split("\n"), key


def test_the_migration_seeded_every_report_row_from_its_original():
    for key in KEYS:
        row, d = _row(key), defaults.ORIGINALS[key]
        assert row.seeded and row.enabled, key
        assert (row.name, row.trigger, row.audience) == (d.name, d.trigger, d.audience)
        assert (row.subject, row.email_body, row.sms_body) == (d.subject, d.email_body, "")
        assert row.recipients == []


def test_seeding_again_never_touches_a_report_row():
    _staff("deposit_report", ["ops@apc.test"])
    MessageTemplate.objects.filter(key="task_digest").update(subject="Edited {overdue_tasks}")
    defaults.seed(keys=KEYS)
    assert _row("deposit_report").staff_emails == ["ops@apc.test"]
    assert _row("task_digest").subject == "Edited {overdue_tasks}"


# --- where the lists came from (migration 0028) ------------------------------------------


def _lists(environ=None, **config):
    values = {"digest_emails": "", "report_emails": "", "alert_emails": ""} | config
    return importlib.import_module(SEED).staff_lists(
        environ or {}, company_email=values.pop("company", "office@apc.test"), **values
    )


def test_every_list_falls_back_to_the_company_email():
    assert _lists() == {key: ["office@apc.test"] for key in KEYS}


def test_the_env_lists_are_read_as_the_settings_read_them():
    got = _lists(
        {
            "DEPOSIT_REPORT_EMAILS": "ops@apc.test, owner@apc.test",
            "TRIP_CONFIRM_REPORT_EMAILS": "reservations@apc.test",
        }
    )
    assert got["deposit_report"] == ["ops@apc.test", "owner@apc.test"]
    assert got["unconfirmed_trips_report"] == ["reservations@apc.test"]


def test_an_env_list_set_to_nothing_stays_nothing():
    """`DEPOSIT_REPORT_EMAILS=` meant "send to nobody" (.env.example); the row keeps it."""
    assert _lists({"DEPOSIT_REPORT_EMAILS": ""})["deposit_report"] == []


def test_the_tasks_and_dispatch_lists_are_parsed_and_fall_back_as_the_screens_did():
    got = _lists(
        digest_emails="a@apc.test,\nb@apc.test",
        alert_emails="c@apc.test; d@apc.test",
    )
    assert got["task_digest"] == ["a@apc.test", "b@apc.test"]
    # Blank report recipients used the digest's.
    assert got["review_report"] == got["complaint_report"] == ["a@apc.test", "b@apc.test"]
    assert got["dispatch_alert"] == ["c@apc.test", "d@apc.test"]
    assert _lists(report_emails="owner@apc.test")["complaint_report"] == ["owner@apc.test"]


def test_no_company_email_means_no_fallback():
    assert _lists(company="") == {key: [] for key in KEYS}


def test_the_migration_runs_before_the_email_fields_are_dropped():
    tasks = importlib.import_module("apps.tasks.migrations.0016_remove_report_emails")
    dispatch = importlib.import_module("apps.dispatch.migrations.0012_remove_alert_emails")
    for migration in (tasks.Migration, dispatch.Migration):
        assert ("messaging", "0028_seed_report_rows") in migration.dependencies


def test_the_lists_left_the_settings_and_the_screens():
    assert not hasattr(django_settings, "DEPOSIT_REPORT_EMAILS")
    assert not hasattr(django_settings, "TRIP_CONFIRM_REPORT_EMAILS")
    names = {f.name for f in TaskConfig._meta.get_fields()}
    assert not names & {"digest_emails", "report_emails"}
    names = {f.name for f in DispatchAlertConfig._meta.get_fields()}
    assert "alert_emails" not in names
    assert "critical_sms" in names


def test_the_settings_screens_no_longer_ask_for_report_emails(client):
    _owner(client)
    tasks = client.get(reverse("task_settings")).content.decode()
    assert 'name="digest_emails"' not in tasks and 'name="report_emails"' not in tasks
    for key in ("task_digest", "review_report", "complaint_report"):
        assert reverse("message_edit", args=[key]) in tasks
    alerts = client.get(reverse("dispatch_alerts")).content.decode()
    assert 'name="alert_emails"' not in alerts
    assert 'name="critical_sms"' in alerts
    assert reverse("message_edit", args=["dispatch_alert"]) in alerts


# --- sending ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_each_report_goes_to_its_rows_staff_emails(key):
    _staff(key, ["first@apc.test", "second@apc.test"])
    _run(ONE[key])
    assert sorted(m.to[0] for m in mail.outbox) == ["first@apc.test", "second@apc.test"]
    tp = TouchPoint.objects.get(kind=key)
    assert (tp.status, tp.lead_id, tp.message_id) == ("sent", None, _row(key).pk)
    assert sorted(d.address for d in tp.deliveries.all()) == ["first@apc.test", "second@apc.test"]
    assert {d.status for d in tp.deliveries.all()} == {Delivery.Status.SENT}


@pytest.mark.parametrize("key", KEYS)
def test_a_report_row_switched_off_sends_nothing(key):
    _staff(key, ["ops@apc.test"])
    MessageTemplate.objects.filter(key=key).update(enabled=False)
    _run(ONE[key])
    assert mail.outbox == []
    assert TouchPoint.objects.get(kind=key).status == TouchPoint.Status.SKIPPED


def test_the_dispatch_texts_stay_with_the_dispatch_alert_settings():
    """Switching the alert email off leaves the critical texts, which keep their numbers on
    the Dispatch alerts screen."""
    MessageTemplate.objects.filter(key="dispatch_alert").update(enabled=False)
    cfg = DispatchAlertConfig.load()
    cfg.critical_sms = "+15715550100"
    cfg.save()
    texts = _run("dispatch_alert_critical")
    assert mail.outbox == []
    assert [c.kwargs["identifier"] for c in texts.call_args_list] == ["+15715550100"]


@pytest.mark.parametrize("key", KEYS)
def test_a_quiet_day_sends_nothing_and_leaves_no_trail(key):
    _run(NONE[key])
    assert mail.outbox == []
    assert not TouchPoint.objects.filter(kind=key).exists()


@pytest.mark.parametrize("key", KEYS)
def test_a_report_with_no_staff_emails_warns_and_sends_nothing(key, caplog):
    _staff(key, [])
    with caplog.at_level("WARNING"):
        _run(ONE[key])
    assert mail.outbox == []
    assert f"{key}: not sent: No recipient" in caplog.text


@pytest.mark.parametrize("off", ["touchpoints", "master"])
def test_reports_ignore_the_automated_switches(settings, off):
    if off == "master":
        NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})
    _staff("deposit_report", ["ops@apc.test"])
    _run(ONE["deposit_report"])
    assert [m.to for m in mail.outbox] == [["ops@apc.test"]]


def test_the_rows_wording_goes_out_around_the_table():
    MessageTemplate.objects.filter(key="deposit_report").update(
        staff_emails=["ops@apc.test"],
        subject="Deposits owed: {orders}",
        email_body="Morning team,\n\n{report_table}\n\nThanks",
    )
    _run(ONE["deposit_report"])
    (message,) = mail.outbox
    assert message.subject == "Deposits owed: 1 order"
    assert message.body.startswith("Morning team,\n")
    assert "Ada Kavanagh" in message.body and message.body.endswith("Thanks")
    html = message.alternatives[0][0]
    assert "Morning team," in html and "Ada Kavanagh" in html


def test_a_failed_delivery_is_logged_and_the_rest_still_go(caplog):
    _staff("deposit_report", ["bad@apc.test", "ops@apc.test"])

    def deliver(*, to, **kw):
        return None if to == "bad@apc.test" else "pm-1"

    with (
        caplog.at_level("WARNING"),
        patch("apps.notifications.email.deliver_html_email", side_effect=deliver),
    ):
        _run(ONE["deposit_report"])
    assert "deposit_report: delivery to bad@apc.test failed" in caplog.text
    statuses = {
        d.address: d.status for d in TouchPoint.objects.get(kind="deposit_report").deliveries.all()
    }
    assert statuses == {"bad@apc.test": "failed", "ops@apc.test": "sent"}


def test_the_html_part_keeps_the_table_and_the_preheader():
    _staff("review_report", ["owner@apc.test"])
    _run("review_report")
    html = mail.outbox[0].alternatives[0][0]
    assert "Low ratings" in html and "Unhappy Customer" in html
    assert "1 of 3 left a review. 1 low rating." in html


# --- fields ------------------------------------------------------------------------------


def _offered(key: str) -> set[str]:
    return {f.key for f in fields.fields_for(key, "staff", key)}


_COMPANY = {"company_name", "company_phone", "company_email"}


def test_each_report_offers_its_own_values_and_its_table():
    assert _offered("deposit_report") == _COMPANY | {
        "report_date",
        "orders",
        "booked_orders",
        "window_days",
        "report_table",
    }
    assert _offered("unconfirmed_trips_report") == _COMPANY | {
        "report_date",
        "customers",
        "trips",
        "trip_count",
        "report_table",
    }
    assert _offered("review_report") == _COMPANY | {
        "period_start",
        "period_end",
        "orders",
        "asked_count",
        "held_count",
        "reviewed_count",
        "trip_page_ratings",
        "report_table",
    }
    assert _offered("complaint_report") == _COMPANY | {
        "new_complaints",
        "period_start",
        "report_table",
    }
    assert _offered("task_digest") == _COMPANY | {
        "report_date",
        "overdue_tasks",
        "tasks",
        "report_table",
    }
    assert _offered("dispatch_alert") == _COMPANY | {
        "report_time",
        "critical_mark",
        "alert_summary",
        "exceptions",
        "critical_note",
        "report_table",
    }


@pytest.mark.parametrize("key", KEYS)
def test_every_original_uses_only_what_its_job_fills(key):
    d = defaults.REPORTS[key]
    used = fields._names(d.subject) | fields._names(d.email_body)
    assert used <= _offered(key), sorted(used - _offered(key))


def test_another_reports_value_is_refused():
    data = listing.row_data(_row("deposit_report")) | {"subject": "{new_complaints}"}
    assert "new_complaints" in fields.validate(data)["subject"]


@pytest.mark.parametrize("key", KEYS)
def test_the_table_must_stay_in_the_report(key):
    data = listing.row_data(_row(key))
    data["email_body"] = data["email_body"].replace("{report_table}", "")
    assert "{report_table}" in fields.validate(data)["email_body"]


@pytest.mark.parametrize("key", KEYS)
def test_every_offered_field_renders_through_a_real_run(key):
    row = _row(key)
    row.email_body += "\n\n" + " ".join("{" + name + "}" for name in sorted(_offered(key)))
    row.staff_emails = ["ops@apc.test"]
    assert fields.validate(listing.row_data(row)) == {}
    row.save()
    _run(ONE[key])
    assert TouchPoint.objects.get(kind=key).status == "sent"
    assert not re.search(r"\{\w+\}", mail.outbox[-1].body)


# --- the editor and the page --------------------------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_a_report_row_is_healthy_and_validates(key):
    row = _row(key)
    row.staff_emails = ["ops@apc.test"]
    assert listing.problems(row) == []
    assert listing.problems(row, master_on=False) == []
    assert fields.validate(listing.row_data(row)) == {}


def test_a_report_row_with_no_staff_emails_says_it_wont_send():
    row = _row("task_digest")
    row.staff_emails = []
    assert listing.problems(row) == [listing.NO_RECIPIENTS]


def test_a_report_row_is_not_edited_just_for_its_list():
    row = _row("complaint_report")
    row.staff_emails = ["someone@apc.test"]
    assert listing.tag(row) == ""
    row.subject = "Complaints: {new_complaints}"
    assert listing.tag(row) == "Edited"


@pytest.mark.parametrize("key", KEYS)
def test_reset_puts_the_wording_back_and_keeps_the_list(key):
    _staff(key, ["ops@apc.test"])
    row = _row(key)
    row.subject = "Edited {company_name}"
    row.enabled = False
    row.save()
    editor.reset(row)
    row.refresh_from_db()
    assert row.subject == defaults.REPORTS[key].subject
    assert row.staff_emails == ["ops@apc.test"]
    assert row.enabled is True


@pytest.mark.parametrize("key", KEYS)
def test_the_staff_emails_and_wording_are_editable(client, key):
    _owner(client)
    _staff(key, ["ops@apc.test"])
    body = _row(key).email_body + "\n\nThanks"
    resp = _edit(client, key, staff_emails=["night@apc.test", "ops@apc.test"], email_body=body)
    assert resp.status_code == 302
    row = _row(key)
    assert row.staff_emails == ["night@apc.test", "ops@apc.test"]
    assert row.email_body == body


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "over",
    [
        {"send_email": None, "send_sms": "on", "sms_body": "Hi"},
        {"send_sms": "on", "sms_body": "Hi"},
        {"trigger": "quote_sent"},
        {"audience": "customer", "recipients": ["booking_contact"]},
        {"service_types": "wedding"},
    ],
    ids=["email-off", "text-on", "trigger", "audience", "service-types"],
)
def test_the_trigger_channels_audience_and_service_types_are_the_reports(client, key, over):
    _owner(client)
    _staff(key, ["ops@apc.test"])
    if over.get("service_types") == "wedding":
        over = {"service_types": [ServiceTypeFactory(name="Wedding").pk]}
    before = listing.row_data(_row(key))
    resp = _edit(client, key, **over)
    assert resp.status_code == 200
    assert escape(editor.SET_BY_REPORT) in resp.content.decode()
    assert listing.row_data(_row(key)) == before


@pytest.mark.parametrize("key", KEYS)
def test_the_editor_shows_the_staff_list_and_no_trigger_audience_or_channel_choice(client, key):
    _owner(client)
    _staff(key, ["ops@apc.test"])
    html = client.get(reverse("message_edit", args=[key])).content.decode()
    assert 'id="staff-email-input"' in html
    assert '<input type="hidden" name="trigger" value="' + key + '">' in html
    assert 'aria-label="Audience"' not in html
    assert 'type="checkbox" name="service_types"' not in html
    assert "ti-lock" in html  # the channel is shown, fixed
    assert not editor.is_locked(_row(key))


@pytest.mark.parametrize("key", KEYS)
def test_a_report_has_a_switch_on_the_page(client, key):
    _owner(client)
    _staff(key, ["ops@apc.test"])
    resp = client.post(reverse("message_toggle", args=[key]))
    assert resp.json() == {"ok": True, "enabled": False}
    assert _row(key).enabled is False


@pytest.mark.parametrize("key", KEYS)
def test_the_preview_renders_every_part_with_samples(key):
    row = _row(key)
    row.staff_emails = ["ops@apc.test"]
    got = editor.preview(row, None)
    assert got["to"] == ["ops@apc.test · email"]
    assert got["email"] and not re.search(r"\{\w+\}", got["email"])
    assert got["sms"] == ""


@pytest.mark.parametrize("key", KEYS)
def test_no_other_row_can_move_onto_a_report_trigger(key):
    assert not editor.can_pick(_row("tp1_welcome"), key)
    assert editor.can_pick(_row(key), key)
    assert not editor.can_pick(_row(key), "quote_sent")


class _Singletons:
    """Stands in for a historical model's manager: `.filter(pk=1).first()`. The real
    tables have lost these columns by now (tasks 0016, dispatch 0012), so the config the
    migration read is faked at the registry; the rows themselves are the real model."""

    def __init__(self, row):
        self.objects = self
        self.row = row

    def filter(self, **_kw):
        return self

    def first(self):
        return self.row


class _Registry:
    def __init__(self, tasks, alerts):
        self.models = {("tasks", "taskconfig"): tasks, ("dispatch", "dispatchalertconfig"): alerts}

    def get_model(self, app, name):
        return (
            self.models.get((app, name.lower()))
            or {
                "MessageTemplate": MessageTemplate,
                "TouchPoint": TouchPoint,
            }.get(name)
            or _live(app, name)
        )


def _live(app, name):
    from django.apps import apps as live

    return live.get_model(app, name)


def _unseeded(monkeypatch):
    for var in ("DEPOSIT_REPORT_EMAILS", "TRIP_CONFIRM_REPORT_EMAILS"):
        monkeypatch.delenv(var, raising=False)
    MessageTemplate.objects.filter(key__in=KEYS).update(seeded=False, staff_emails=[])


def test_the_migration_reads_the_tasks_and_dispatch_singletons(monkeypatch, settings):
    """`seed_rows` fills each fresh report row from the Tasks and Dispatch alerts config
    the way the screens read them (the branch a fresh test DB never reaches, with no
    singletons yet)."""
    from types import SimpleNamespace

    seed = importlib.import_module(SEED)
    settings.COMPANY_EMAIL = "office@apc.test"
    _unseeded(monkeypatch)
    registry = _Registry(
        _Singletons(SimpleNamespace(digest_emails="a@apc.test\nb@apc.test", report_emails="")),
        _Singletons(SimpleNamespace(alert_emails="c@apc.test; d@apc.test")),
    )
    seed.seed_rows(registry, None)
    got = {key: _row(key).staff_emails for key in KEYS}
    digest = ["a@apc.test", "b@apc.test"]
    assert got == {
        "task_digest": digest,
        "review_report": digest,
        "complaint_report": digest,
        "dispatch_alert": ["c@apc.test", "d@apc.test"],
        "deposit_report": ["office@apc.test"],
        "unconfirmed_trips_report": ["office@apc.test"],
    }


def test_the_migration_uses_the_report_list_and_leaves_edited_rows_alone(monkeypatch, settings):
    from types import SimpleNamespace

    seed = importlib.import_module(SEED)
    settings.COMPANY_EMAIL = "office@apc.test"
    _unseeded(monkeypatch)
    MessageTemplate.objects.filter(key="task_digest").update(
        seeded=True, staff_emails=["edited@apc.test"]
    )
    registry = _Registry(
        _Singletons(SimpleNamespace(digest_emails="a@apc.test", report_emails="r@apc.test")),
        _Singletons(None),
    )
    seed.seed_rows(registry, None)
    assert _row("review_report").staff_emails == ["r@apc.test"]
    assert _row("complaint_report").staff_emails == ["r@apc.test"]
    assert _row("dispatch_alert").staff_emails == ["office@apc.test"]
    assert _row("task_digest").staff_emails == ["edited@apc.test"]
