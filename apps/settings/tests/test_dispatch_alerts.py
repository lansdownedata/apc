"""APC-23 — the Dispatch-alerts settings screen (owner-admin only, singleton form)."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch.models import DispatchAlertConfig

pytestmark = pytest.mark.django_db


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def test_screen_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))

    resp = client.get(reverse("dispatch_alerts"))

    assert resp.status_code in (302, 403)


def test_screen_shows_the_current_thresholds(client):
    _owner(client)

    resp = client.get(reverse("dispatch_alerts"))

    assert resp.status_code == 200
    assert resp.context["form"].instance.pk == 1
    assert b"24" in resp.content  # default unassigned_warn_hours


def test_saving_updates_the_singleton(client):
    _owner(client)

    resp = client.post(
        reverse("dispatch_alerts"),
        {
            "enabled": "on",
            "unassigned_warn_hours": 36,
            "unassigned_critical_hours": 6,
            "otw_warn_minutes": 45,
            "otw_critical_minutes": 15,
            "arrived_warn_minutes": 15,
            "arrived_critical_minutes": 45,
            "driver_info_warn_hours": 24,
            "driver_info_critical_hours": 6,
            "affiliate_unacked_warn_hours": 24,
            "critical_sms": "+15715550100",
        },
    )

    assert resp.status_code == 302
    cfg = DispatchAlertConfig.load()
    assert cfg.unassigned_warn_hours == 36
    assert cfg.critical_sms == "+15715550100"
    assert DispatchAlertConfig.objects.count() == 1


def test_the_settings_menu_links_to_it(client):
    _owner(client)

    body = client.get(reverse("settings_index")).content.decode()

    assert reverse("dispatch_alerts") in body
    assert "Dispatch alerts" in body


def test_driver_details_setting_says_when_it_takes_effect(client):
    _owner(client)

    resp = client.get(reverse("dispatch_alerts"))

    assert (
        "Takes effect the next time driver details are saved for a trip." in resp.content.decode()
    )


def test_every_threshold_has_a_written_label():
    """SET-14: no auto-generated "Otw warn minutes" or "Critical sms"."""
    from apps.settings.forms import DispatchAlertConfigForm

    labels = {name: str(f.label) for name, f in DispatchAlertConfigForm().fields.items()}

    assert labels["unassigned_warn_hours"] == "No coverage warning (hours)"
    assert labels["unassigned_critical_hours"] == "No coverage critical (hours)"
    assert labels["otw_warn_minutes"] == "Not en route warning (minutes)"
    assert labels["otw_critical_minutes"] == "Not en route critical (minutes)"
    assert labels["arrived_warn_minutes"] == "Not arrived warning (minutes)"
    assert labels["arrived_critical_minutes"] == "Not arrived critical (minutes)"
    assert labels["driver_info_warn_hours"] == "Driver info missing warning (hours)"
    assert labels["driver_info_critical_hours"] == "Driver info missing critical (hours)"
    assert labels["affiliate_unacked_warn_hours"] == "Affiliate unacknowledged warning (hours)"
    assert labels["critical_sms"] == "Critical SMS numbers"
