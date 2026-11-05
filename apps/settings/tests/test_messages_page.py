import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.factories import UserFactory
from apps.messaging.defaults import seed
from apps.messaging.factories import MessageRowFactory
from apps.messaging.models import MessageTemplate, NotificationConfig

pytestmark = pytest.mark.django_db


def _admin(client):
    client.force_login(UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN))


def _get(client, **params):
    return client.get(reverse("messages"), params).content.decode()


def test_agent_is_forbidden(client):
    client.force_login(UserFactory())
    assert client.get(reverse("messages")).status_code == 403
    assert client.get(reverse("messages_new")).status_code == 403
    assert client.post(reverse("messages_quiet_hours")).status_code == 403


def test_sections_and_rows_render(client):
    _admin(client)
    seed()
    html = _get(client)
    for label in MessageTemplate.Section.labels:
        assert escape(label) in html
    for row in MessageTemplate.objects.live():
        assert row.name in html
    assert reverse("message_toggle", args=["tp1_welcome"]) in html
    assert reverse("message_edit", args=["tp1_welcome"]) in html


def test_status_dispatched_listed_with_switch(client):
    _admin(client)
    seed()
    row = MessageTemplate.objects.get(key="status_dispatched")
    html = _get(client)
    assert row.name in html
    assert reverse("message_toggle", args=["status_dispatched"]) in html


def test_staff_click_row_has_no_switch(client):
    _admin(client)
    row = MessageRowFactory(name="Send quote", trigger="send_quote", recipients=["booking_contact"])
    html = _get(client)
    assert "Sent when staff click Send quote" in html
    assert reverse("message_toggle", args=[row.key]) not in html


def test_problem_note_and_heading(client):
    _admin(client)
    MessageRowFactory(name="Broken one", recipients=[], section="trip_day", trigger="quote_sent")
    html = _get(client)
    assert "No recipients ticked, so it won&#x27;t send" in html or "so it won't send" in html
    assert "1 needs attention" in html


def test_master_off_problem(client):
    _admin(client)
    MessageRowFactory(name="Fine one", recipients=["booking_contact"])
    NotificationConfig.objects.update_or_create(singleton_id=1, defaults={"enabled": False})
    html = _get(client)
    assert "On, but all messaging is off" in html
    assert "All messaging off" in html


def test_filters_hide_empty_sections_and_empty_state(client):
    _admin(client)
    MessageRowFactory(name="Findable thing", section="leads_quotes")
    html = _get(client, q="findable")
    assert "Findable thing" in html
    assert "After the trip" not in html
    html = _get(client, q="zzzz-nothing")
    assert "No messages match" in html


def test_filter_values_stay_selected(client):
    _admin(client)
    MessageRowFactory(name="Off one", enabled=False)
    html = _get(client, state="off", q="Off")
    assert "Off one" in html
    assert 'value="Off"' in html


def test_new_placeholder_and_quiet_hours_endpoint(client):
    _admin(client)
    assert client.get(reverse("messages_new")).status_code == 200
    html = _get(client)
    assert reverse("messages_new") in html
    assert reverse("messages_quiet_hours") in html


def test_archive_added_row_only(client):
    _admin(client)
    added = MessageRowFactory(name="Mine")
    seed()
    seeded = MessageTemplate.objects.get(key="tp1_welcome")
    assert client.get(reverse("message_archive", args=[added.key])).status_code == 405
    assert client.post(reverse("message_archive", args=[added.key])).status_code == 302
    added.refresh_from_db()
    assert added.archived_at is not None
    # Built-in messages are reset, never archived: there is no archive for them (404).
    assert client.post(reverse("message_archive", args=[seeded.key])).status_code == 404
    seeded.refresh_from_db()
    assert seeded.archived_at is None
    assert ">Mine<" not in _get(client)


def test_query_count_bounded(client):
    _admin(client)
    seed()
    for _ in range(15):
        MessageRowFactory()
    with CaptureQueriesContext(connection) as ctx:
        _get(client)
    assert len(ctx) <= 20


def test_toggle_works_for_an_added_row(client):
    import json

    _admin(client)
    row = MessageRowFactory(name="Mine", enabled=True)
    resp = client.post(reverse("message_toggle", args=[row.key]))
    assert json.loads(resp.content) == {"ok": True, "enabled": False}
    row.refresh_from_db()
    assert row.enabled is False
    assert client.post(reverse("message_toggle", args=["no-such-key"])).status_code == 404


def test_toggle_refuses_a_staff_click_row(client):
    _admin(client)
    row = MessageRowFactory(name="Send quote", trigger="send_quote", enabled=True)
    assert client.post(reverse("message_toggle", args=[row.key])).status_code == 400
    row.refresh_from_db()
    assert row.enabled is True
