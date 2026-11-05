"""The message editor (spec C section 6): one page to change who gets a message, when, for
which service types and what it says; add, archive and reset; and its live preview.

Ported from spec B's `test_message_edit.py`: those tests keep their expectations, the setup
now posts the whole row. Podium and Postmark are patched wherever a send could happen.
"""

import json
from datetime import date, time, timedelta
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging import defaults, editor, listing, rules
from apps.messaging import rows as message_rows
from apps.messaging.factories import MessageRowFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.touchpoint_templates import TEMPLATES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

TRANSPORTS = (
    "apps.integrations.podium.send_message",
    "apps.integrations.podium.create_review_invitation",
    "apps.notifications.email.deliver_html_email",
    "apps.notifications.email.send_html_email",
)


def _owner_admin():
    return UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN)


def _booked_lead(name="Dana Smith", contact=None, **trip):
    lead = LeadFactory(contact=contact or ContactFactory(name=name), status=Lead.Status.BOOKED)
    trip.setdefault("pickup_date", date(2027, 3, 12))
    trip.setdefault("pickup_time", time(9, 30))
    trip.setdefault("pickup_timezone", "America/Chicago")
    ReservationFactory(lead=lead, **trip)
    return lead


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def post_data(row: MessageTemplate, **over) -> dict:
    """The editor form as the browser posts it for this row; ``over`` replaces fields
    (None drops one: an unticked checkbox). ``timing=(amount, unit)`` sets both."""
    data = {
        "name": row.name,
        "section": row.section,
        "audience": row.audience,
        "trigger": row.trigger,
        "timing_amount": row.timing_amount,
        "timing_unit": row.timing_unit,
        "timing_direction": row.timing_direction,
        "subject": row.subject,
        "email_body": row.email_body,
        "sms_body": row.sms_body,
        "recipients": list(row.recipients or []),
        "staff_emails": list(row.staff_emails or []),
        "service_types": [st.pk for st in row.service_types.all()] if row.pk else [],
    }
    for flag in ("send_sms", "send_email", "enabled"):
        if getattr(row, flag):
            data[flag] = "on"
    if "timing" in over:
        timing = over.pop("timing")
        if timing is None:
            data.pop("timing_amount")
        else:
            data["timing_amount"], data["timing_unit"] = timing
    for name, value in over.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    return data


def _post_data(kind, **over):
    """Spec B's helper: the message's default wording and timing, switched on."""
    default = defaults.DEFAULTS[kind]
    base = {
        "subject": default.subject,
        "email_body": default.email_body,
        "sms_body": default.sms_body,
        "enabled": "on",
        "timing": (default.timing_amount, default.timing_unit),
    }
    return post_data(_row(kind), **(base | over))


def _edited(key: str) -> bool:
    return listing.tag(_row(key)) == "Edited"


# --- ported from spec B's editor ----------------------------------------------------------


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
    MessageTemplate.objects.filter(key="driver_released").update(
        sms_body="Hi {first_name} {driver_name}"
    )
    html = client.get(reverse("message_edit", args=["driver_released"])).content.decode()
    assert "Hi {first_name} {driver_name}" in html
    assert "Driver cell" in html
    # Spec B asserted "Payment link" here; spec C's pickup trigger can fill payment links
    # (`fields.fields_for`), so a field it can't fill stands in: the affiliate payout.
    assert "Payout" not in html
    html = client.get(reverse("message_edit", args=["tp1_welcome"])).content.decode()
    assert "Quote link" in html
    assert "Driver cell" not in html


def test_untimed_message_has_no_timing_inputs(client):
    """Spec B: a message that sends when it happens had no timing. In spec C that is a staff
    click (or schedule); event triggers carry a timing."""
    client.force_login(_owner_admin())
    row = MessageRowFactory(name="Quote email", trigger="send_quote", recipients=["planner"])
    html = client.get(reverse("message_edit", args=[row.key])).content.decode()
    assert "Sent when staff click Send quote" in html
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
    row = _row("tp1_welcome")
    assert row.sms_body == "Hello {first_name}"
    assert row.timing_amount == 5


def test_save_normalises_browser_newlines(client):
    client.force_login(_owner_admin())
    default = TEMPLATES["tp1_welcome"]
    client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data(
            "tp1_welcome",
            sms_body=default.sms_body.replace("\n", "\r\n"),
            email_body=default.email_body.replace("\n", "\r\n"),
        ),
    )
    assert not _edited("tp1_welcome")
    assert _row("tp1_welcome").sms_body == default.sms_body


def test_save_can_turn_the_message_off(client):
    client.force_login(_owner_admin())
    data = _post_data("tp1_welcome")
    del data["enabled"]
    client.post(reverse("message_edit", args=["tp1_welcome"]), data)
    assert _row("tp1_welcome").enabled is False


def test_save_invalid_shows_box_error_and_keeps_text(client):
    client.force_login(_owner_admin())
    before = _row("tp1_welcome").sms_body
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", sms_body="Hello {nope} there"),
    )
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Unknown field {nope}" in html
    assert "Hello {nope} there" in html
    # Spec B started from an empty table and asserted nothing was written; rows are never
    # deleted now, so: the stored row is untouched.
    assert _row("tp1_welcome").sms_body == before


def test_save_invalid_timing_shows_error(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", timing_amount="99999"),
    )
    assert "Use 0 to 1440 minutes." in resp.content.decode()


def test_first_confirmation_editor_has_no_second_timing(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("message_edit", args=["trip_confirm_customer"])).content.decode()
    assert "timing2" not in html
    assert "Second notice" not in html


def test_second_notice_editor_edits_wording_and_timing(client):
    client.force_login(_owner_admin())
    url = reverse("message_edit", args=["trip_confirm_customer_2"])
    html = client.get(url).content.decode()
    assert 'name="timing_amount"' in html
    assert "Uses the Trip confirmation switch" not in html
    assert 'aria-label="Trip confirmation — second notice switch"' in html
    data = _post_data(
        "trip_confirm_customer_2",
        sms_body="Please confirm {trip_pickup_when} {confirm_link}",
        timing_amount="36",
        timing_unit="hours",
    )
    del data["enabled"]
    assert client.post(url, data).status_code == 302
    assert message_rows.timing_of("trip_confirm_customer_2") == (36, "hours")
    row = _row("trip_confirm_customer_2")
    assert row.sms_body.startswith("Please confirm")


def test_wedding_thank_you_editor_has_its_own_timing_inputs(client):
    client.force_login(_owner_admin())
    url = reverse("message_edit", args=["thank_you_wedding"])
    html = client.get(url).content.decode()
    assert "Sent with the Thank-you timing" not in html
    assert 'name="timing_amount"' in html
    data = _post_data("thank_you_wedding", timing_amount="5", timing_unit="hours")
    assert client.post(url, data).status_code == 302
    assert message_rows.timing_of("thank_you_wedding") == (5, "hours")
    assert message_rows.timing_of("thank_you") == (2, "hours")


def test_subject_over_200_characters_is_refused(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_edit", args=["tp1_welcome"]),
        _post_data("tp1_welcome", subject="x" * 201),
    )
    assert resp.status_code == 200
    assert "Keep the subject under 200 characters." in resp.content.decode()


def test_preview_renders_real_order(client):
    client.force_login(_owner_admin())
    lead = _booked_lead()
    resp = client.post(
        reverse("message_preview", args=["trip_confirm_customer"]),
        _post_data("trip_confirm_customer", lead=lead.pk),
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
        _post_data("tp1_welcome", subject="S", email_body="E", sms_body="Draft for {first_name}"),
    )
    assert json.loads(resp.content)["sms"] == "Draft for Sample"


def test_preview_sample_when_no_lead(client):
    client.force_login(_owner_admin())
    resp = client.post(reverse("message_preview", args=["tp1_welcome"]), _post_data("tp1_welcome"))
    body = json.loads(resp.content)
    assert body["ok"] is True
    assert body["source"] == "Sample order"
    assert body["sms"]


def test_preview_bad_draft_is_400_with_errors(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("message_preview", args=["tp1_welcome"]),
        _post_data("tp1_welcome", subject="S", email_body="E", sms_body="Hello {nope}"),
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


def test_reset_restores_the_default(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Hi {first_name}")
    resp = client.post(reverse("message_reset", args=["tp1_welcome"]))
    assert resp.status_code == 302
    assert not _edited("tp1_welcome")
    row = _row("tp1_welcome")
    assert row.sms_body == TEMPLATES["tp1_welcome"].sms_body
    assert client.get(reverse("message_reset", args=["tp1_welcome"])).status_code == 405


def test_toggle_returns_400_when_the_saved_text_no_longer_validates(client):
    client.force_login(_owner_admin())
    MessageTemplate.objects.filter(key="tp1_welcome").update(sms_body="Hi {gone_field}")
    resp = client.post(reverse("message_toggle", args=["tp1_welcome"]))
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert body["ok"] is False
    assert "gone_field" in body["error"]


class TestPreviewOrders:
    """Spec B picked orders by message kind; spec C by the trigger's level and the row's
    service types (a wedding message is a row with the Wedding service type)."""

    def test_booked_only_newest_first(self):
        old = _booked_lead("Old")
        new = _booked_lead("New")
        LeadFactory(contact=ContactFactory(name="Open"), status=Lead.Status.NEW)
        assert editor.preview_orders("lead_created") == [new, old]

    def test_trip_kinds_need_a_live_trip(self):
        cancelled = _booked_lead(trip_status="cancelled")
        live = _booked_lead()
        assert editor.preview_orders("pickup") == [live]
        assert cancelled in editor.preview_orders("lead_created")

    def test_wedding_kinds_only_wedding_orders(self):
        wedding_type = ServiceTypeFactory(name=WEDDING_SERVICE_NAME)
        plain = _booked_lead()
        wedding = _booked_lead(service_type=wedding_type)
        wedding.wedding_name = "Kim & Lee"
        wedding.save()
        assert editor.preview_orders("pickup", [wedding_type]) == [wedding]
        assert editor.preview_orders("all_trips_done", [wedding_type]) == [wedding]
        assert plain in editor.preview_orders("all_trips_done")

    def test_limit(self):
        for i in range(3):
            _booked_lead(f"P{i}")
        assert len(editor.preview_orders("lead_created", limit=2)) == 2


@pytest.mark.parametrize("kind", sorted(defaults.DEFAULTS))
def test_every_message_opens_and_previews_default_text(client, kind):
    client.force_login(_owner_admin())
    _booked_lead()
    assert client.get(reverse("message_edit", args=[kind])).status_code == 200
    row = _row(kind)
    orders = editor.preview_orders(row.trigger, row.service_types.all())
    for lead in ("", orders[0].pk if orders else ""):
        resp = client.post(reverse("message_preview", args=[kind]), _post_data(kind, lead=lead))
        assert resp.status_code == 200, resp.content


def test_preview_sends_nothing_and_creates_no_touchpoint(client):
    client.force_login(_owner_admin())
    lead = _booked_lead()
    before = TouchPoint.objects.count()
    data = _post_data("trip_confirm_customer")
    patches = [mock.patch(target) for target in TRANSPORTS]
    mocks = [p.start() for p in patches]
    try:
        editor.preview(_row("trip_confirm_customer"), lead)
        client.post(
            reverse("message_preview", args=["trip_confirm_customer"]), {**data, "lead": lead.pk}
        )
        client.post(reverse("message_preview", args=["trip_confirm_customer"]), data)
    finally:
        for p in patches:
            p.stop()
    for m in mocks:
        m.assert_not_called()
    assert TouchPoint.objects.count() == before


def test_editor_has_error_state_and_sample_label(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("message_edit", args=["tp1_welcome"])).content.decode()
    assert "data-sample-label" in html
    assert "Sample order" in html
    js = open("static/js/app.js").read()
    assert "Preview unavailable. Check the text and try again." in js
    assert "Example unavailable." in js


# --- the spec C editor ----------------------------------------------------------------


def test_page_sections_and_seeded_controls(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("message_edit", args=["driver_released"])).content.decode()
    for heading in ("Who gets it", "When it goes out", "Service types", "Wording", "Preview"):
        assert heading in html
    assert "On this order it would go to" in html
    assert "Built-in" in html
    assert "Reset to original" in html
    assert "Archive" not in html
    assert "Day-of coordinator" in html and "Wedding contacts" in html


def test_who_gets_it_copy_matches_the_mockup():
    js = open("static/js/app.js").read()
    for copy in (
        "the lead passenger if one is marked, else every passenger with a mobile, else the "
        "booking contact",
        "Farmed-in orders send to the sending operator instead.",
        "Nobody is ticked, so this message won't send.",
        "Passengers and drivers only get texts, so they're hidden while this message is "
        "email-only.",
    ):
        assert copy in js


def test_seeded_trigger_change_warning_lists_the_built_in_rules(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("message_edit", args=["driver_released"])).content.decode()
    assert "This built-in message loses its built-in rules" in html
    for rule in rules.built_in_descriptions("driver_released"):
        assert escape(rule) in html
    assert 'x-show="triggerChanged" style="display: none"' in html
    MessageTemplate.objects.filter(key="driver_released").update(trigger="status_dispatched")
    html = client.get(reverse("message_edit", args=["driver_released"])).content.decode()
    assert 'x-show="triggerChanged" style="display: none"' not in html
    assert "This built-in message loses its built-in rules" in html


def test_added_row_has_archive_not_reset(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(name="Mine", recipients=["booking_contact"], sms_body="Hi")
    html = client.get(reverse("message_edit", args=[row.key])).content.decode()
    assert "Archive" in html
    assert "Reset to original" not in html
    assert "Added" in html
    assert "This built-in message loses its built-in rules" not in html


def test_save_stamps_updated_by(client):
    user = _owner_admin()
    client.force_login(user)
    client.post(reverse("message_edit", args=["tp1_welcome"]), _post_data("tp1_welcome"))
    assert _row("tp1_welcome").updated_by == user


def test_audience_must_be_allowed_by_the_trigger(client):
    client.force_login(_owner_admin())
    row = _row("tp1_welcome")
    for trigger, audience in (("affiliate_accepted", "planner"), ("planner_invite", "customer")):
        resp = client.post(
            reverse("message_edit", args=["tp1_welcome"]),
            post_data(row, trigger=trigger, audience=audience),
        )
        assert resp.status_code == 200
        assert "This trigger can&#x27;t send to that audience." in resp.content.decode()
    assert _row("tp1_welcome").trigger == "lead_created"


def test_recipients_must_suit_the_audience(client):
    client.force_login(_owner_admin())
    row = _row("trip_confirm_affiliate")
    resp = client.post(
        reverse("message_edit", args=["trip_confirm_affiliate"]),
        post_data(row, recipients=["company", "booking_contact"]),
    )
    assert resp.status_code == 200
    assert "Pick people this audience can reach." in resp.content.decode()
    assert _row("trip_confirm_affiliate").recipients == ["company"]


def test_save_validation_on_channels_recipients_and_before(client):
    client.force_login(_owner_admin())
    row = _row("tp1_welcome")
    url = reverse("message_edit", args=["tp1_welcome"])
    html = client.post(url, post_data(row, send_sms=None, send_email=None)).content.decode()
    assert "Turn on Text or Email." in html
    html = client.post(url, post_data(row, recipients=None)).content.decode()
    assert "Tick at least one person." in html
    html = client.post(url, post_data(row, timing_direction="before")).content.decode()
    assert "Before only works with a date." in html
    html = client.post(url, post_data(row, subject="")).content.decode()
    assert "Add a subject for the email." in html
    assert _row("tp1_welcome").send_sms is True


def test_text_only_people_are_dropped_from_an_email_only_message(client):
    client.force_login(_owner_admin())
    row = _row("driver_released")
    client.post(
        reverse("message_edit", args=["driver_released"]),
        post_data(row, send_sms=None, recipients=["passengers", "booking_contact"]),
    )
    assert _row("driver_released").recipients == ["booking_contact"]


def test_staff_audience_saves_staff_emails(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(
        name="Report",
        section="staff_reports",
        audience="staff",
        trigger="deposit_report",
        send_sms=False,
        subject="Deposits",
        email_body="Body",
        staff_emails=["a@example.com"],
    )
    client.post(
        reverse("message_edit", args=[row.key]),
        post_data(row, staff_emails=["b@example.com, c@example.com", "b@example.com"]),
    )
    row.refresh_from_db()
    assert row.staff_emails == ["b@example.com", "c@example.com"]
    resp = client.post(reverse("message_edit", args=[row.key]), post_data(row, staff_emails="x"))
    assert "Enter a valid email address." in resp.content.decode()


# --- what a save does to queued sends (Review Focus 4) -----------------------------------


def _queued(row, lead=None, at=None):
    return TouchPointFactory(
        lead=lead or LeadFactory(),
        message=row,
        kind=row.key,
        status="scheduled",
        scheduled_for=at or timezone.now() + timedelta(days=1),
    )


def test_changed_timing_redates_queued_sends(client):
    client.force_login(_owner_admin())
    row = _row("tp2_lead_followup")
    at = timezone.now() + timedelta(hours=1)
    tp = _queued(row, at=at)
    resp = client.post(
        reverse("message_edit", args=["tp2_lead_followup"]),
        post_data(row, timing=(5, "hours")),
        follow=True,
    )
    assert "1 scheduled message re-dated." in resp.content.decode()
    tp.refresh_from_db()
    assert tp.scheduled_for == at + timedelta(hours=3)


def test_switching_off_cancels_queued_sends(client):
    client.force_login(_owner_admin())
    row = _row("tp2_lead_followup")
    tp = _queued(row)
    client.post(reverse("message_edit", args=["tp2_lead_followup"]), post_data(row, enabled=None))
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.CANCELLED


def test_toggle_off_cancels_queued_sends(client):
    client.force_login(_owner_admin())
    tp = _queued(_row("tp2_lead_followup"))
    assert client.post(reverse("message_toggle", args=["tp2_lead_followup"])).status_code == 200
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.CANCELLED


def test_trigger_change_cancels_and_queues_the_date_for_booked_orders(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(
        name="Heads-up",
        section="trip_day",
        trigger="quote_sent",
        recipients=["booking_contact"],
        send_email=False,
        sms_body="See you {pickup_date}",
    )
    old = _queued(row)
    soon = timezone.now() + timedelta(days=10)
    local = soon.astimezone(timezone.get_current_timezone())
    booked = LeadFactory(status=Lead.Status.BOOKED)
    upcoming = ReservationFactory(lead=booked, pickup_date=local.date(), pickup_time=time(15, 0))
    gone = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=local.date(),
        pickup_time=time(15, 0),
        trip_status="cancelled",
    )
    quoted = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.QUOTED),
        pickup_date=local.date(),
        pickup_time=time(15, 0),
    )
    with mock.patch(TRANSPORTS[0]):
        resp = client.post(
            reverse("message_edit", args=[row.key]),
            post_data(row, trigger="pickup", timing=(2, "days"), timing_direction="before"),
        )
    assert resp.status_code == 302
    old.refresh_from_db()
    assert old.status == TouchPoint.Status.CANCELLED
    made = TouchPoint.objects.filter(message=row, status="scheduled")
    assert [(tp.lead_id, tp.reservation_id) for tp in made] == [(booked.pk, upcoming.pk)]
    assert made.get().scheduled_for == upcoming.pickup_at - timedelta(days=2)
    assert not TouchPoint.objects.filter(message=row, reservation__in=[gone, quoted]).exists()


def test_trigger_change_to_an_event_queues_nothing(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(
        name="Heads-up", trigger="quote_sent", recipients=["booking_contact"], sms_body="Hi"
    )
    _booked_lead()
    client.post(
        reverse("message_edit", args=[row.key]),
        post_data(row, trigger="order_confirmed", send_email=None),
    )
    assert _row(row.key).trigger == "order_confirmed"
    assert not TouchPoint.objects.filter(message=row, status="scheduled").exists()


# --- new, archive, reset --------------------------------------------------------------------


def test_new_message_page_presets_the_section(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages_new"), {"section": "trip_day"}).content.decode()
    assert "New message" in html
    assert '<option value="trip_day" selected>' in html
    assert "Reset to original" not in html


def test_new_message_is_saved_as_an_added_row(client):
    client.force_login(_owner_admin())
    blank = MessageTemplate(
        name="Coordinator heads-up",
        section="trip_day",
        trigger="status_on_the_way",
        recipients=["role:day_of_coordinator"],
        send_email=False,
        sms_body="Your chauffeur {driver_name} is on the way.",
        enabled=True,
    )
    wedding = ServiceTypeFactory(name=WEDDING_SERVICE_NAME)
    resp = client.post(
        reverse("messages_new"), post_data(blank, timing=(0, "minutes"), service_types=[wedding.pk])
    )
    assert resp.status_code == 302
    row = MessageTemplate.objects.get(name="Coordinator heads-up")
    assert row.key == f"custom-{row.pk}"
    assert row.seeded is False
    assert row.recipients == ["role:day_of_coordinator"]
    assert list(row.service_types.all()) == [wedding]


def test_archive_keeps_the_row_and_cancels_its_queued_sends(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(name="Mine", recipients=["booking_contact"], sms_body="Hi")
    tp = _queued(row)
    assert client.get(reverse("message_archive", args=[row.key])).status_code == 405
    assert client.post(reverse("message_archive", args=[row.key])).status_code == 302
    row.refresh_from_db()
    assert row.archived_at is not None
    assert row.enabled is False
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.CANCELLED
    assert not MessageTemplate.objects.live().filter(pk=row.pk).exists()
    assert ">Mine<" not in client.get(reverse("messages")).content.decode()
    assert client.get(reverse("message_edit", args=[row.key])).status_code == 404


def test_archiving_a_row_with_no_sends_still_keeps_it(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(name="Unused")
    client.post(reverse("message_archive", args=[row.key]))
    assert MessageTemplate.objects.filter(pk=row.pk).exists()
    assert not MessageTemplate.objects.live().filter(pk=row.pk).exists()


def test_seeded_rows_are_never_archived_and_added_never_reset(client):
    client.force_login(_owner_admin())
    added = MessageRowFactory(name="Mine")
    assert client.post(reverse("message_archive", args=["tp1_welcome"])).status_code == 404
    assert _row("tp1_welcome").archived_at is None
    assert client.post(reverse("message_reset", args=[added.key])).status_code == 404


def test_reset_restores_switch_recipients_and_withdraws(client):
    client.force_login(_owner_admin())
    row = _row("status_arrived")
    MessageTemplate.objects.filter(pk=row.pk).update(
        enabled=True, timing_amount=5, recipients=["passengers"]
    )
    tp = _queued(row, at=timezone.now() + timedelta(hours=5))
    resp = client.post(reverse("message_reset", args=["status_arrived"]), follow=True)
    row.refresh_from_db()
    assert (row.enabled, row.timing_amount, row.recipients) == (False, 0, ["booking_contact"])
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.CANCELLED  # reset switched it back off
    assert "Reset to the original." in resp.content.decode()


# --- staff clicks and preview recipients ---------------------------------------------------


def test_staff_click_row_has_no_switch_and_stays_on(client):
    client.force_login(_owner_admin())
    row = MessageRowFactory(
        name="Quote email",
        trigger="send_quote",
        recipients=["booking_contact"],
        subject="Your quote",
        email_body="{quote_link}",
        send_sms=False,
    )
    html = client.get(reverse("message_edit", args=[row.key])).content.decode()
    assert "Sent when staff click Send quote" in html
    assert 'x-show="!isClick" style="display: none"' in html
    resp = client.post(reverse("message_edit", args=[row.key]), post_data(row, enabled=None))
    assert resp.status_code == 302
    row.refresh_from_db()
    assert row.enabled is True


def test_preview_lists_who_it_would_go_to(client):
    client.force_login(_owner_admin())
    booker = ContactFactory(name="Dana Whitfield", phone="+12025550147", email="d@example.com")
    lead = _booked_lead(contact=booker)
    coordinator = ContactFactory(name="Maria Lopez", phone="", email="maria@example.com")
    LeadContact.objects.create(lead=lead, contact=coordinator, role="day_of_coordinator")
    row = _row("trip_confirm_customer")
    with mock.patch(TRANSPORTS[0]) as send:
        resp = client.post(
            reverse("message_preview", args=[row.key]),
            post_data(row, recipients=["booking_contact", "role:day_of_coordinator"], lead=lead.pk),
        )
    send.assert_not_called()
    body = json.loads(resp.content)
    assert body["to"] == [
        "Dana Whitfield (booking contact) · text and email",
        "Maria Lopez (Day-of coordinator) · email",
    ]
    assert body["sms_to"] == "Dana Whitfield (booking contact)"
    assert {f["key"] for f in body["fields"]} >= {"first_name", "trip_pickup_when"}


def test_each_section_links_to_a_new_message_in_it(client):
    client.force_login(_owner_admin())
    html = client.get(reverse("messages")).content.decode()
    for section in MessageTemplate.Section.values:
        assert f"{reverse('messages_new')}?section={section}" in html
