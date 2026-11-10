"""The Settings Overview: what needs attention first, then the status of each switch."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.models import DispatchAlertConfig
from apps.fleet.factories import DriverFactory, RenewalFactory, VehicleFactory
from apps.integrations.factories import PodiumCredentialFactory
from apps.integrations.models import QboCredential
from apps.messaging.models import NotificationConfig
from apps.settings.menu import label_for
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db

# Measured with every attention row on (all configs off, a lapsed driver, QuickBooks and
# Podium keys set but not connected, not live): a page that grows past this should fail loudly.
QUERY_CAP = 16

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
}


@pytest.fixture(autouse=True)
def all_clear(settings):
    """Nothing needs attention unless a test turns it on; QuickBooks and Podium dark unless set."""
    for model in (NotificationConfig, TaskConfig, DispatchAlertConfig):
        _set(model, True)
    settings.OUTBOUND_ENABLED = True
    settings.QBO_CLIENT_ID = ""
    settings.QBO_CLIENT_SECRET = ""
    settings.PODIUM_CLIENT_ID = ""
    settings.PODIUM_LOCATION_UID = ""


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client


def _set(model, enabled: bool) -> None:
    cfg = model.load()
    cfg.enabled = enabled
    cfg.save()


def _qbo_keys(settings) -> None:
    for key, value in QBO.items():
        setattr(settings, key, value)


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


def _lapsed_driver() -> None:
    RenewalFactory(driver=DriverFactory(), expires_on=timezone.localdate() - timedelta(days=1))


def _body(owner) -> str:
    resp = owner.get(reverse("settings_index"))
    assert resp.status_code == 200
    return resp.content.decode()


def test_header_copy(owner):
    html = _body(owner)

    assert "<h1" in html and ">Settings</h1>" in html
    assert "How the portal prices, dispatches and talks to customers." in html
    assert "Needs attention</h2>" in html
    assert "Status</h2>" in html


def test_attention_rows_link_to_their_page(owner):
    _set(NotificationConfig, False)

    html = _body(owner)

    assert "Automated messages are off" in html
    assert "No customer or affiliate touch-points send until this is on." in html
    assert f'href="{reverse("messages")}"' in html
    assert "Messages →" in html
    assert "Nothing needs attention." not in html


def test_attention_row_severity_dots(owner, settings):
    settings.OUTBOUND_ENABLED = False
    _set(TaskConfig, False)

    html = _body(owner)
    section = html[html.index("Needs attention</h2>") : html.index("Status</h2>")]

    assert section.count("bg-danger") == 1
    assert section.count("bg-warn") == 1


def test_not_live_row_has_no_link(owner, settings):
    settings.OUTBOUND_ENABLED = False

    html = _body(owner)
    section = html[html.index("Needs attention</h2>") : html.index("Status</h2>")]

    assert "Not live" in section
    assert "Customers and affiliates receive nothing." in section
    assert "Turned on once, at go-live, by your developer." in section
    assert "<a " not in section


def test_fleet_row_links_to_renewal_types(owner):
    _lapsed_driver()

    html = _body(owner)

    assert "1 driver or vehicle has lapsed or expiring paperwork" in html
    assert f'href="{reverse("renewal_type_list")}"' in html
    assert "Renewal types →" in html


def test_empty_state(owner, settings):
    DriverFactory()  # nothing on file: not attention
    settings.PODIUM_CLIENT_ID = "podium-client"
    PodiumCredentialFactory()  # configured and connected: not attention
    html = _body(owner)

    assert "Nothing needs attention." in html
    assert "→" not in html[html.index("Needs attention</h2>") : html.index("Status</h2>")]


def test_status_rows(owner, settings):
    settings.OUTBOUND_ENABLED = False
    _set(DispatchAlertConfig, False)
    _qbo_keys(settings)
    _connect_qbo()
    settings.PODIUM_CLIENT_ID = "podium-client"
    PodiumCredentialFactory(location_uid="loc-1")

    resp = owner.get(reverse("settings_index"))

    assert resp.context["status"] == [
        ("Go-live", "warn", "Not live"),
        ("Automated messages", "ok", "On"),
        ("Dispatch monitor", "danger", "Off"),
        ("Tasks", "ok", "On"),
        ("QuickBooks", "ok", "Connected"),
        ("Podium", "ok", "Connected · loc-1"),
    ]


def test_status_rows_all_the_other_way(owner, settings):
    _set(NotificationConfig, False)
    _set(TaskConfig, False)
    _qbo_keys(settings)
    settings.PODIUM_CLIENT_ID = "podium-client"

    resp = owner.get(reverse("settings_index"))

    assert resp.context["status"] == [
        ("Go-live", "ok", "Live"),
        ("Automated messages", "danger", "Off"),
        ("Dispatch monitor", "ok", "On"),
        ("Tasks", "danger", "Off"),
        ("QuickBooks", "danger", "Not connected"),
        ("Podium", "danger", "Not connected"),
    ]


def test_status_has_no_quickbooks_row_without_keys(owner):
    labels = [label for label, _, _ in owner.get(reverse("settings_index")).context["status"]]

    assert "QuickBooks" not in labels


def test_status_has_no_podium_row_without_keys(owner):
    PodiumCredentialFactory(location_uid="loc-1")
    labels = [label for label, _, _ in owner.get(reverse("settings_index")).context["status"]]

    assert "Podium" not in labels


def test_podium_status_falls_back_to_the_location_setting(owner, settings):
    settings.PODIUM_CLIENT_ID = "podium-client"
    settings.PODIUM_LOCATION_UID = "loc-env"
    PodiumCredentialFactory(location_uid="")

    status = owner.get(reverse("settings_index")).context["status"]

    assert ("Podium", "ok", "Connected · loc-env") in status


def test_overview_names_its_list_settings_attention(owner):
    _set(NotificationConfig, False)

    resp = owner.get(reverse("settings_index"))

    assert [a.key for a in resp.context["settings_attention"]] == ["messages_off"]


def test_status_text_colours(owner, settings):
    """Not live is amber, like its attention row; every other off switch is red."""
    settings.OUTBOUND_ENABLED = False
    _set(DispatchAlertConfig, False)

    html = _body(owner)
    status = html[html.index("Status</h2>") :]

    assert '<dd class="text-warn font-semibold">Not live</dd>' in status
    assert '<dd class="text-danger font-semibold">Off</dd>' in status
    assert '<dd class="text-ok">On</dd>' in status


def test_overview_query_count(owner, settings, django_assert_max_num_queries):
    for model in (NotificationConfig, TaskConfig, DispatchAlertConfig):
        _set(model, False)
    settings.OUTBOUND_ENABLED = False
    _qbo_keys(settings)
    settings.PODIUM_CLIENT_ID = "podium-client"
    _lapsed_driver()
    VehicleFactory()

    with django_assert_max_num_queries(QUERY_CAP) as captured:
        html = _body(owner)

    assert "lapsed or expiring paperwork" in html
    assert "Podium is not connected" in html
    # The menu reuses the view's attention list: the fleet is read once, not twice.
    driver_reads = [q for q in captured.captured_queries if "FROM `fleet_driver`" in q["sql"]]
    assert len(driver_reads) == 1


def test_label_for_returns_the_menu_label():
    assert label_for("messages") == "Messages"
    assert label_for("renewal_type_list") == "Renewal types"
    assert label_for("task_settings") == "Tasks"
    assert label_for("dispatch_alerts") == "Dispatch alerts"


def test_label_for_knows_quickbooks_even_without_keys():
    """The QuickBooks row only exists with keys set, but the label never depends on that."""
    assert label_for("quickbooks_settings") == "QuickBooks"


def test_label_for_an_unknown_page_raises():
    """A link labelled with a guess would be a silent bug; fail where it is built."""
    with pytest.raises(KeyError):
        label_for("no_such_page")
