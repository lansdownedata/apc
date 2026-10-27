import json
from datetime import date, time

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import overrides
from apps.messaging.models import MessageTemplate
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _owner_admin():
    return UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN)


def _booked_lead(name="Dana Smith", **trip):
    lead = LeadFactory(contact=ContactFactory(name=name), status=Lead.Status.BOOKED)
    trip.setdefault("pickup_date", date(2027, 3, 12))
    trip.setdefault("pickup_time", time(9, 30))
    trip.setdefault("pickup_timezone", "America/Chicago")
    ReservationFactory(lead=lead, **trip)
    return lead


def _post_data(kind, **over):
    default = overrides.default(kind)
    data = {
        "subject": default.subject,
        "email_body": default.email_body,
        "sms_body": default.sms_body,
        "enabled": "on",
    }
    timing = overrides.timing_of(kind)
    if timing:
        data["timing_amount"], data["timing_unit"] = timing
    data.update(over)
    return data


def test_requires_owner_admin(client):
    client.force_login(UserFactory())
    assert client.get(reverse("message_edit", args=["tp1_welcome"])).status_code == 403
    assert client.post(reverse("message_edit", args=["tp1_welcome"])).status_code == 403


def test_unknown_kind_404(client):
    client.force_login(_owner_admin())
    assert client.get(reverse("message_edit", args=["nope"])).status_code == 404
    assert client.post(reverse("message_reset", args=["nope"])).status_code == 404
    assert client.post(reverse("message_preview", args=["nope"])).status_code == 404
    assert client.get(reverse("message_timing_preview", args=["nope"])).status_code == 404


def test_renders_current_text_and_chips(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.create(kind="driver_released", sms_body="Hi {first_name} {driver_name}")
    html = client.get(reverse("message_edit", args=["driver_released"])).content.decode()
    assert "Hi {first_name} {driver_name}" in html
    assert "Driver cell" in html
    assert "Payment link" not in html
    html = client.get(reverse("message_edit", args=["tp1_welcome"])).content.decode()
    assert "Quote link" in html
    assert "Driver cell" not in html


def test_triggered_message_has_no_timing_inputs(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("message_edit", args=["order_confirmed"])).content.decode()
    assert "Sent when it happens" in html
    assert 'name="timing_amount"' not in html
    html = client.get(reverse("message_edit", args=["tp1_welcome"])).content.decode()
    assert 'name="timing_amount"' in html


def test_save_valid_redirects_with_toast(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", sms_body="Hello {first_name}", timing_amount="5"),
        follow=True,
    )
    assert resp.redirect_chain[-1][0] == reverse("messages")
    assert "Saved. Applies to messages not yet sent." in resp.content.decode()
    row = MessageTemplate.objects.get(kind="tp1_welcome")
    assert row.sms_body == "Hello {first_name}"
    assert row.timing_amount == 5


def test_save_normalises_browser_newlines(client):
    client.force_login(_owner_admin())
    default = overrides.default("tp1_welcome")
    client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data(
            "tp1_welcome",
            sms_body=default.sms_body.replace("\n", "\r\n"),
            email_body=default.email_body.replace("\n", "\r\n"),
        ),
    )
    assert not MessageTemplate.objects.filter(kind="tp1_welcome").exists()


def test_save_can_turn_the_message_off(client):
    client.force_login(_owner_admin())
    data = _post_data("tp1_welcome")
    del data["enabled"]
    client.post(reverse("message_edit", args=["tp1_welcome"]), data)
    assert overrides.is_enabled("tp1_welcome") is False


def test_save_invalid_shows_box_error_and_keeps_text(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", sms_body="Hello {nope} there"),
    )
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Unknown field {nope}" in html
    assert "Hello {nope} there" in html
    assert not MessageTemplate.objects.filter(kind="tp1_welcome").exists()


def test_save_invalid_timing_shows_error(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", timing_amount="99999"),
    )
    assert "Use 0 to 1440 minutes." in resp.content.decode()


def test_second_notice_timing_on_confirmation_editor(client):
    client.force_login(_owner_admin())
    url = reverse("message_edit", args=["trip_confirm_customer"])
    html = client.get(url).content.decode()
    assert 'name="timing2_amount"' in html
    data = _post_data("trip_confirm_customer", timing2_amount="36", timing2_unit="hours")
    assert client.post(url, data).status_code == 302
    assert overrides.timing_of("trip_confirm_customer_2") == (36, "hours")
    assert MessageTemplate.objects.get(kind="trip_confirm_customer_2").timing_amount == 36


def test_preview_renders_real_order(client):
    client.force_login(_owner_admin())
    lead = _booked_lead()
    default = overrides.default("trip_confirm_customer")
    resp = client.post(
        reverse("message_preview", args=["trip_confirm_customer"]),
        {
            "subject": default.subject,
            "email_body": default.email_body,
            "sms_body": default.sms_body,
            "lead": lead.pk,
        },
    )
    body = json.loads(resp.content)
    assert body["ok"] is True
    assert "Dana" in body["sms"]
    assert "CST" in body["sms"] or "CDT" in body["sms"]
    assert lead.quote_no in body["email"]
    assert body["source"] == lead.quote_no


def test_preview_uses_the_draft_text(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_preview", args=["tp1_welcome"]),
        {"subject": "S", "email_body": "E", "sms_body": "Draft for {first_name}"},
    )
    assert json.loads(resp.content)["sms"] == "Draft for Sample"


def test_preview_sample_when_no_lead(client):
    client.force_login(_owner_admin())
    default = overrides.default("tp1_welcome")
    resp = client.post(
        reverse("message_preview", args=["tp1_welcome"]),
        {
            "subject": default.subject,
            "email_body": default.email_body,
            "sms_body": default.sms_body,
        },
    )
    body = json.loads(resp.content)
    assert body["ok"] is True
    assert body["source"] == "Sample order"
    assert body["sms"]


def test_preview_bad_draft_is_400_with_errors(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_preview", args=["tp1_welcome"]),
        {"subject": "S", "email_body": "E", "sms_body": "Hello {nope}"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["ok"] is False
    assert "sms_body" in body["errors"]


def test_timing_preview(client):
    client.force_login(_owner_admin())
    resp = client.get(
        reverse("message_timing_preview", args=["tp1_welcome"]), {"amount": 5, "unit": "minutes"}
    )
    body = json.loads(resp.content)
    assert body["ok"] is True
    assert "sends" in body["text"]
    resp = client.get(
        reverse("message_timing_preview", args=["tp1_welcome"]), {"amount": "x", "unit": "minutes"}
    )
    assert resp.status_code == 400
    assert json.loads(resp.content)["ok"] is False


def test_reset_deletes_row(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hi {first_name}")
    resp = client.post(reverse("message_reset", args=["tp1_welcome"]))
    assert resp.status_code == 302
    assert not MessageTemplate.objects.filter(kind="tp1_welcome").exists()
    assert client.get(reverse("message_reset", args=["tp1_welcome"])).status_code == 405


def test_toggle_returns_400_when_the_saved_text_no_longer_validates(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hi {gone_field}")
    resp = client.post(reverse("message_toggle", args=["tp1_welcome"]))
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["ok"] is False
    assert "gone_field" in body["error"]


class TestPreviewOrders:
    def test_booked_only_newest_first(self):
        old = _booked_lead("Old")
        new = _booked_lead("New")
        LeadFactory(contact=ContactFactory(name="Open"), status=Lead.Status.NEW)
        assert overrides.preview_orders("tp1_welcome") == [new, old]

    def test_trip_kinds_need_a_live_trip(self):
        cancelled = _booked_lead(trip_status="cancelled")
        live = _booked_lead()
        assert overrides.preview_orders("trip_confirm_customer") == [live]
        assert cancelled in overrides.preview_orders("tp1_welcome")

    def test_wedding_kinds_only_wedding_orders(self):
        plain = _booked_lead()
        wedding = _booked_lead()
        wedding.wedding_name = "Kim & Lee"
        wedding.save()
        assert overrides.preview_orders("wed_final_details") == [wedding]
        assert overrides.preview_orders("thank_you_wedding") == [wedding]
        assert plain in overrides.preview_orders("thank_you")

    def test_limit(self):
        for i in range(3):
            _booked_lead(f"P{i}")
        assert len(overrides.preview_orders("tp1_welcome", limit=2)) == 2


@pytest.mark.parametrize("kind", [m.kind for m in overrides.MESSAGES])
def test_every_message_opens_and_previews_default_text(client, kind):
    client.force_login(_owner_admin())
    _booked_lead()
    assert client.get(reverse("message_edit", args=[kind])).status_code == 200
    default = overrides.default(kind)
    for lead in (
        "",
        overrides.preview_orders(kind)[0].pk if overrides.preview_orders(kind) else "",
    ):
        resp = client.post(
            reverse("message_preview", args=[kind]),
            {
                "subject": default.subject,
                "email_body": default.email_body,
                "sms_body": default.sms_body,
                "lead": lead,
            },
        )
        assert resp.status_code == 200, resp.content
