"""Inside Settings the sidebar becomes a grouped, searchable settings menu."""

import re

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch.models import DispatchAlertConfig
from apps.messaging.models import NotificationConfig
from apps.settings.menu import current_item, sections
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
}


@pytest.fixture(autouse=True)
def all_clear(settings):
    """No attention rows unless a test turns one on, and QuickBooks dark unless set."""
    for model in (NotificationConfig, TaskConfig, DispatchAlertConfig):
        cfg = model.load()
        cfg.enabled = True
        cfg.save()
    settings.OUTBOUND_ENABLED = True
    settings.QBO_CLIENT_ID = ""
    settings.QBO_CLIENT_SECRET = ""


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client


def _overview(owner) -> str:
    resp = owner.get(reverse("settings_index"))
    assert resp.status_code == 200
    return resp.content.decode()


def _anchor(html: str, label: str) -> str:
    """The opening `<a …>` tag of the menu item titled `label`."""
    match = re.search(rf'<a\b[^>]*\btitle="{re.escape(label)}"[^>]*>', html)
    assert match, f"no menu item titled {label!r}"
    return match.group(0)


def _labels() -> list[str]:
    return [item.label for section in sections() for item in section.items]


def test_overview_renders_the_settings_menu(owner):
    html = _overview(owner)

    assert "Back to portal" in html
    assert 'aria-label="Search settings"' in html
    assert "Vehicle types" in html
    assert reverse("pricing") in html


def test_overview_is_current(owner):
    html = _overview(owner)

    assert "nav-active" in _anchor(html, "Overview")
    assert "nav-active" not in _anchor(html, "Vehicle types")


def test_dashboard_keeps_the_portal_menu(owner):
    html = owner.get(reverse("dashboard")).content.decode()

    assert 'aria-label="Search settings"' not in html
    assert reverse("pipeline") in html


def test_current_item_matches_prefixes():
    assert current_item("vehicle_type_edit").label == "Vehicle types"
    assert current_item("message_edit").label == "Messages"
    assert current_item("messages_quiet_hours").label == "Messages"
    assert current_item("dashboard") is None
    assert current_item(None) is None


def test_keywords_are_searchable(owner):
    tag = _anchor(_overview(owner), "Vehicle types")

    search = re.search(r'data-search="([^"]*)"', tag).group(1)
    assert search.startswith("vehicle types ")
    assert "cleaning fee" in search


def test_flagged_item_has_a_dot(owner):
    cfg = NotificationConfig.load()
    cfg.enabled = False
    cfg.save()

    assert 'title="Automated messages are off"' in _overview(owner)


def test_no_dot_when_fine(owner):
    assert 'title="Automated messages are off"' not in _overview(owner)


def test_quickbooks_only_with_keys(settings):
    assert "QuickBooks" not in _labels()

    for key, value in QBO.items():
        setattr(settings, key, value)

    assert "QuickBooks" in _labels()


def test_no_match_state_present(owner):
    assert "No settings match" in _overview(owner)
