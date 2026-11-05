import json

import pytest
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape

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
    labels = [escape(label) for label in MessageTemplate.Section.labels]
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions)
    welcome = html.index("Welcome message")
    assert positions[0] < welcome < positions[1]
    assert "30 minutes after lead comes in · Text and Email · Booking contact" in html
    assert "12 hours before pickup, once dispatched" in html
    assert "1 day after all trips done · Text · Booking contact" in html
    assert "Uses the Thank-you switch" not in html
    assert "Thank-you (wedding) switch" in html


def test_catalogue_kinds_are_known_and_unique():
    kinds = [m.kind for m in overrides.MESSAGES]
    assert len(kinds) == len(set(kinds))
    assert kinds.index("trip_confirm_customer_2") == kinds.index("trip_confirm_customer") + 1
    for kind in kinds:
        overrides.default(kind)


def test_edited_badge(client):
    client.force_login(_owner_admin())
    assert "Edited" not in client.get(reverse("messages")).content.decode()
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Hi")
    assert "Edited" in client.get(reverse("messages")).content.decode()


def test_timing_reflects_an_override(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.filter(key="tp1_welcome").update(timing_amount=5, timing_unit="minutes")
    assert (
        "5 minutes after lead comes in · Text and Email"
        in client.get(reverse("messages")).content.decode()
    )


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
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Hello {first_name}")
    client.post(reverse("message_toggle", args=["tp1_welcome"]))
    row = MessageTemplate.objects.get(key="tp1_welcome")
    assert row.sms_body == "Hello {first_name}"
    assert row.enabled is False


def test_toggle_unknown_kind_404(client):
    client.force_login(_owner_admin())
    assert client.post(reverse("message_toggle", args=["nope"])).status_code == 404


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
    MessageTemplate.objects.filter(key="tp1_welcome").update(send_sms=True, send_email=False)
    html = client.get(reverse("messages")).content.decode()
    assert "30 minutes after lead comes in · Text · " in html
    MessageTemplate.objects.filter(key="tp1_welcome").update(send_sms=False, send_email=True)
    html = client.get(reverse("messages")).content.decode()
    assert "30 minutes after lead comes in · Email · " in html
    MessageTemplate.objects.filter(key="tp1_welcome").update(send_sms=True, send_email=True)
    html = client.get(reverse("messages")).content.decode()
    assert "30 minutes after lead comes in · Text and Email · " in html


def test_second_notice_row_uses_the_confirmation_switch(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages")).content.decode()
    assert "Trip confirmation — second notice" in html
    assert "(also sent as the second notice)" not in html
    assert "Uses the Trip confirmation switch" not in html
    assert "Trip confirmation — second notice switch" in html
    assert "Sent with the Thank-you timing" not in html
    for kind in ("trip_confirm_customer_2", "thank_you_wedding"):
        assert client.post(reverse("message_toggle", args=[kind])).status_code == 200
        assert MessageTemplate.objects.get(key=kind).enabled is False
    assert MessageTemplate.objects.get(key="trip_confirm_customer").enabled is True
    assert MessageTemplate.objects.get(key="thank_you").enabled is True
