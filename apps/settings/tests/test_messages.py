import json

import pytest
from django.urls import NoReverseMatch, reverse

from apps.accounts.factories import UserFactory
from apps.messaging import overrides
from apps.messaging.models import MessageTemplate, NotificationConfig

pytestmark = pytest.mark.django_db


def _owner_admin():
    return UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN)


def test_requires_owner_admin(client):
    client.force_login(UserFactory())
    assert client.get(reverse("messages")).status_code == 403


def test_lists_groups_and_rows(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages")).content.decode()
    for spec in overrides.MESSAGES:
        assert spec.label in html
    assert "Before the trip" in html
    assert "12 hours before pickup, once Dispatched" in html
    assert "30 min after the lead comes in" in html
    assert "2 hours after the lead comes in" in html
    assert "1 day before the quote expires" in html
    assert "When it happens" in html
    assert "Uses the Thank-you switch" in html


def test_catalogue_kinds_are_known_and_unique():
    kinds = [m.kind for m in overrides.MESSAGES]
    assert len(kinds) == len(set(kinds))
    assert kinds.index("trip_confirm_customer_2") == kinds.index("trip_confirm_customer") + 1
    for kind in kinds:
        overrides.default(kind)


def test_edited_badge(client):
    client.force_login(_owner_admin())
    assert "Edited" not in client.get(reverse("messages")).content.decode()
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hi")
    assert "Edited" in client.get(reverse("messages")).content.decode()


def test_timing_reflects_an_override(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.create(kind="tp1_welcome", timing_amount=5, timing_unit="minutes")
    assert "5 min after the lead comes in" in client.get(reverse("messages")).content.decode()


def test_toggle_flips_and_persists(client):
    client.force_login(_owner_admin())
    resp = client.post(reverse("message_toggle", args=["tp1_welcome"]))
    assert resp.status_code == 200
    assert json.loads(resp.content) == {"ok": True, "enabled": False}
    assert overrides.is_enabled("tp1_welcome") is False
    resp = client.post(reverse("message_toggle", args=["tp1_welcome"]))
    assert json.loads(resp.content) == {"ok": True, "enabled": True}
    assert overrides.is_enabled("tp1_welcome") is True


def test_toggle_keeps_edited_text(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hello {first_name}")
    client.post(reverse("message_toggle", args=["tp1_welcome"]))
    row = MessageTemplate.objects.get(kind="tp1_welcome")
    assert row.sms_body == "Hello {first_name}"
    assert row.enabled is False


def test_toggle_unknown_kind_404(client):
    client.force_login(_owner_admin())
    assert client.post(reverse("message_toggle", args=["nope"])).status_code == 404
    assert client.post(reverse("message_toggle", args=["thank_you_wedding"])).status_code == 404


def test_toggle_requires_owner_admin_and_post(client):
    client.force_login(UserFactory())
    assert client.post(reverse("message_toggle", args=["tp1_welcome"])).status_code == 403
    client.force_login(_owner_admin())
    assert client.get(reverse("message_toggle", args=["tp1_welcome"])).status_code == 405


def test_master_switch(client):
    client.force_login(_owner_admin())
    resp = client.post(reverse("messages_master"))
    assert json.loads(resp.content) == {"ok": True, "enabled": False}
    assert NotificationConfig.load().enabled is False
    resp = client.post(reverse("messages_master"))
    assert json.loads(resp.content) == {"ok": True, "enabled": True}
    assert NotificationConfig.load().enabled is True


def test_old_notifications_url_gone():
    with pytest.raises(NoReverseMatch):
        reverse("notifications")


def test_index_card_links_to_messages(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("settings_index")).content.decode()
    assert reverse("messages") in html
    assert "Customer notifications" not in html


def test_rows_show_their_channels(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages")).content.decode()
    assert "Email &amp; SMS" in html
    assert ">Email<" in html
    assert ">SMS<" in html


def test_second_notice_row_uses_the_confirmation_switch(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages")).content.decode()
    assert "Trip confirmation — second notice" in html
    assert "(also sent as the second notice)" not in html
    assert "Uses the Trip confirmation switch" in html
    assert "Sent with the Thank-you timing" in html
    assert (
        client.post(reverse("message_toggle", args=["trip_confirm_customer_2"])).status_code == 404
    )
