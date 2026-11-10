"""Inside Settings the sidebar becomes a grouped, searchable settings menu."""

import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.addresses.factories import VenueFactory
from apps.dispatch.models import DispatchAlertConfig
from apps.fleet.factories import DriverFactory, VehicleFactory
from apps.integrations.models import QboCredential
from apps.leads.factories import VehicleTypeFactory
from apps.messaging.models import MessageTemplate, NotificationConfig
from apps.settings.menu import current_item, sections
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db

QUERY_CAP = 15

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


def _connect_quickbooks(settings) -> None:
    for key, value in QBO.items():
        setattr(settings, key, value)
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


def _menu_items() -> list:
    return [item for section in sections() for item in section.items]


def test_every_menu_page_renders_the_menu_with_itself_current(owner, settings):
    _connect_quickbooks(settings)
    items = _menu_items()
    assert "Users" in [i.label for i in items] and "QuickBooks" in [i.label for i in items]

    for item in items:
        resp = owner.get(reverse(item.url_name))
        html = resp.content.decode()
        assert resp.status_code == 200, f"{item.label}: status {resp.status_code}"
        assert 'aria-label="Search settings"' in html, f"{item.label}: no menu"
        assert "nav-active" in _anchor(html, item.label), f"{item.label}: not current"


def test_edit_pages_use_the_menu(owner):
    pages = {
        "Vehicle types": reverse("vehicle_type_edit", args=[VehicleTypeFactory().pk]),
        "Points of interest": reverse("poi_edit", args=[VenueFactory().pk]),
        "Messages": reverse("message_edit", args=[MessageTemplate.objects.live().first().key]),
        "Users": reverse("user_detail", args=[UserFactory().pk]),
    }
    for label, url in pages.items():
        resp = owner.get(url)
        html = resp.content.decode()
        assert resp.status_code == 200, f"{label}: status {resp.status_code}"
        assert 'aria-label="Search settings"' in html, f"{label}: no menu"
        assert "nav-active" in _anchor(html, label), f"{label}: not current"


def test_no_settings_back_links_remain(owner, settings):
    _connect_quickbooks(settings)
    back = re.compile(
        rf'<a\b[^>]*href="{re.escape(reverse("settings_index"))}"[^>]*>\s*<i[^>]*ti-arrow-left'
    )
    for item in _menu_items():
        html = owner.get(reverse(item.url_name)).content.decode()
        assert not back.search(html), f"{item.label}: still has a back link to Settings"


def test_users_left_the_main_sidebar(owner):
    html = owner.get(reverse("dashboard")).content.decode()

    assert reverse("user_list") not in html


def test_menu_query_cost_on_a_list_page(owner, django_assert_max_num_queries):
    DriverFactory()
    VehicleFactory()
    with django_assert_max_num_queries(QUERY_CAP):
        assert owner.get(reverse("vehicle_type_list")).status_code == 200
