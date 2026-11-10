"""What needs attention — one list feeding the Overview page and the menu's red dots."""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.dispatch.models import DispatchAlertConfig
from apps.fleet.factories import DriverFactory, RenewalFactory
from apps.integrations.models import QboCredential
from apps.messaging.models import NotificationConfig
from apps.settings.attention import Attention, settings_attention
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
}


@pytest.fixture(autouse=True)
def all_clear(settings):
    """Every condition false unless a test turns one on."""
    for model in (NotificationConfig, TaskConfig, DispatchAlertConfig):
        cfg = model.load()
        cfg.enabled = True
        cfg.save()
    settings.OUTBOUND_ENABLED = True
    settings.QBO_CLIENT_ID = ""
    settings.QBO_CLIENT_SECRET = ""


def _keys() -> list[str]:
    return [a.key for a in settings_attention()]


def _row(key: str) -> Attention:
    return next(a for a in settings_attention() if a.key == key)


def _off(model) -> None:
    cfg = model.load()
    cfg.enabled = False
    cfg.save()


def _lapsed_driver():
    d = DriverFactory()
    RenewalFactory(driver=d, expires_on=timezone.localdate() - timedelta(days=1))
    return d


def _qbo_keys(settings) -> None:
    for k, v in QBO.items():
        setattr(settings, k, v)


def _connect_qbo() -> None:
    cred = QboCredential.load()
    cred.realm_id = "9130"
    cred.set_tokens(
        access="at",
        refresh="rt",
        access_expires_at=timezone.now() + timedelta(hours=1),
        refresh_expires_at=None,
    )
    cred.connected_at = timezone.now()
    cred.save()


def test_nothing_needs_attention_when_all_clear():
    assert settings_attention() == []


def test_messages_off():
    _off(NotificationConfig)
    row = _row("messages_off")
    assert row.title == "Automated messages are off"
    assert row.detail == "No customer or affiliate touch-points send until this is on."
    assert (row.url_name, row.severity) == ("messages", "danger")


def test_messages_on_has_no_row():
    assert "messages_off" not in _keys()


def test_tasks_off():
    _off(TaskConfig)
    row = _row("tasks_off")
    assert row.title == "Tasks are off"
    assert row.detail == "No tasks are created or escalated until this is on."
    assert (row.url_name, row.severity) == ("task_settings", "danger")


def test_tasks_on_has_no_row():
    assert "tasks_off" not in _keys()


def test_dispatch_off():
    _off(DispatchAlertConfig)
    row = _row("dispatch_off")
    assert row.title == "Dispatch monitor is off"
    assert row.detail == "Missed-milestone alerts will not fire."
    assert (row.url_name, row.severity) == ("dispatch_alerts", "danger")


def test_dispatch_on_has_no_row():
    assert "dispatch_off" not in _keys()


def test_fleet_plural():
    _lapsed_driver()
    _lapsed_driver()
    row = _row("fleet")
    assert row.title == "2 drivers or vehicles have lapsed or expiring paperwork"
    assert row.detail == "Renewals on file are expired or due soon."
    assert (row.url_name, row.severity) == ("renewal_type_list", "danger")


def test_one_subject_reads_singular():
    _lapsed_driver()
    assert _row("fleet").title == "1 driver or vehicle has lapsed or expiring paperwork"


def test_fleet_clear_has_no_row():
    DriverFactory()
    assert "fleet" not in _keys()


def test_qbo_keys_set_but_not_connected(settings):
    _qbo_keys(settings)
    row = _row("qbo")
    assert row.title == "QuickBooks is not connected"
    assert row.detail == "Billing accounts and invoices cannot sync until it is connected."
    assert (row.url_name, row.severity) == ("quickbooks_settings", "danger")


def test_qbo_connected_has_no_row(settings):
    _qbo_keys(settings)
    _connect_qbo()
    assert "qbo" not in _keys()


def test_qbo_ignored_without_keys(settings):
    settings.QBO_CLIENT_ID = ""
    _connect_qbo()
    assert QboCredential.objects.exists()
    assert "qbo" not in _keys()


def test_not_live(settings):
    settings.OUTBOUND_ENABLED = False
    row = _row("not_live")
    assert row.title == "Not live"
    assert row.detail == (
        "Customers and affiliates receive nothing. Turned on once, at go-live, by your developer."
    )


def test_not_live_has_no_link():
    with override_settings(OUTBOUND_ENABLED=False):
        row = _row("not_live")
    assert row.url_name is None
    assert row.severity == "warn"


def test_live_has_no_not_live_row():
    assert "not_live" not in _keys()


def test_order_is_fixed(settings):
    for model in (NotificationConfig, TaskConfig, DispatchAlertConfig):
        _off(model)
    _lapsed_driver()
    _qbo_keys(settings)
    settings.OUTBOUND_ENABLED = False
    assert _keys() == [
        "messages_off",
        "tasks_off",
        "dispatch_off",
        "fleet",
        "qbo",
        "not_live",
    ]
