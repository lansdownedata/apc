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
            "alert_emails": "ops@allprocharter.com",
            "critical_sms": "",
        },
    )

    assert resp.status_code == 302
    cfg = DispatchAlertConfig.load()
    assert cfg.unassigned_warn_hours == 36
    assert cfg.alert_emails == "ops@allprocharter.com"
    assert DispatchAlertConfig.objects.count() == 1


def test_the_settings_index_links_to_it(client):
    _owner(client)

    body = client.get(reverse("settings_index")).content.decode()

    assert reverse("dispatch_alerts") in body
    assert "Dispatch alerts" in body
