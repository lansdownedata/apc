"""The staff-click sends as message rows (spec C section 8, APC-120).

Send quote, Send payment link, Send terms, the trip receipt and the affiliate offer and
cancellation each send a seeded row through `sender.send_now`: the wording comes from the
row, the generated parts (the quote summary, the receipt lines, the trip sheets, the button)
from their callers as blocks. What each one sends word for word is held by
`test_staff_click_golden.py`; this file holds the rows, their fields and the trail.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone
from django.utils.html import escape

from apps.contacts.factories import ContactFactory
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import defaults, editor, fields, listing, sender, triggers
from apps.messaging.models import Delivery, Message, MessageTemplate, TouchPoint
from apps.messaging.recipients import Recipient
from apps.payments import receipts
from apps.payments import services as payments
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

SEND = "apps.integrations.podium.send_message"
BASE_URL = "https://portal.example.com"
CLICK_KEYS = (
    "send_quote",
    "send_quote_on_account",
    "send_pay_link",
    "send_terms_link",
    "payment_receipt",
    "affiliate_offer",
    "affiliate_cancel",
)


@pytest.fixture(autouse=True)
def live(settings):
    settings.OUTBOUND_ENABLED = True
    settings.COMPANY_PHONE = "(703) 555-0100"


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _quotable():
    contact = {"name": "Dana Whitfield", "email": "dana@example.com", "phone": "+12025550101"}
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(**contact))
    ReservationFactory(lead=lead, rate=Decimal("185.00"))
    return lead


def _booked(**contact):
    values = {"name": "Dana Whitfield", "email": "dana@example.com", "phone": "+12025550101"}
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(**(values | contact)))
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=9),
        pickup_time=time(14, 0),
        stops=["The Hay-Adams", "Dulles International"],
    )
    return lead, trip


# --- the seeded rows -------------------------------------------------------------------


def test_the_migration_seeded_every_click_row_from_its_original():
    for key in CLICK_KEYS:
        row, d = _row(key), defaults.ORIGINALS[key]
        assert row.seeded is True and row.enabled is True, key
        assert triggers.get(row.trigger).kind == "staff_click", key
        assert (row.trigger, row.audience, row.section, row.name) == (
            d.trigger,
            d.audience,
            d.section,
            d.name,
        ), key
        assert (row.subject, row.email_body, row.sms_body) == (d.subject, d.email_body, d.sms_body)
        assert (row.send_sms, row.send_email) == (d.send_sms, d.send_email), key
        assert row.recipients == list(d.recipients), key
        assert not listing.edited(row), key


def test_the_click_originals_sit_beside_the_touch_point_defaults():
    assert set(defaults.CLICKS) == set(CLICK_KEYS)
    assert not set(defaults.CLICKS) & set(defaults.DEFAULTS)
    assert defaults.ORIGINALS == {**defaults.DEFAULTS, **defaults.CLICKS}


def test_each_click_sends_on_its_own_trigger():
    assert {k: defaults.CLICKS[k].trigger for k in CLICK_KEYS} == {
        "send_quote": "send_quote",
        "send_quote_on_account": "send_quote",
        "send_pay_link": "send_pay_link",
        "send_terms_link": "send_terms_link",
        "payment_receipt": "payment_receipt",
        "affiliate_offer": "affiliate_offer",
        "affiliate_cancel": "affiliate_cancel",
    }


@pytest.mark.parametrize("key", CLICK_KEYS)
def test_a_click_row_has_no_problem_and_validates(key):
    row = _row(key)
    assert listing.problems(row) == []
    assert fields.validate(listing.row_data(row)) == {}


@pytest.mark.parametrize("key", CLICK_KEYS)
def test_reset_puts_a_click_row_back(key):
    row = _row(key)
    row.email_body = row.sms_body = "Edited {company_name}"
    row.save()
    editor.reset(row)
    row.refresh_from_db()
    assert (row.email_body, row.sms_body) == (
        defaults.CLICKS[key].email_body,
        defaults.CLICKS[key].sms_body,
    )
    assert row.enabled is True


def test_reseeding_never_touches_a_click_row():
    MessageTemplate.objects.filter(key="send_quote").update(sms_body="Edited {quote_link}")
    defaults.seed(keys=CLICK_KEYS)
    assert _row("send_quote").sms_body == "Edited {quote_link}"


# --- fields ------------------------------------------------------------------------------


def _offered(trigger, audience="customer"):
    return {f.key for f in fields.fields_for(trigger, audience)}


def test_generated_parts_are_fields_of_the_triggers_that_fill_them():
    assert {"quote_summary", "button", "contact_name", "quote_total"} <= _offered("send_quote")
    assert "payment_for" in _offered("send_pay_link")
    assert "terms_link" in _offered("send_terms_link")
    assert {"receipt_lines", "receipt_intro", "receipt_explanation"} <= _offered("payment_receipt")
    assert {"trip_sheet", "trip_day"} <= _offered("affiliate_offer", "affiliate")
    assert {"cancelled_trip_sheet", "trip_day"} <= _offered("affiliate_cancel", "affiliate")
    for trigger in ("quote_sent", "pickup", "lead_created"):
        assert not _offered(trigger) & {"quote_summary", "receipt_lines", "payment_for"}
    assert "cancelled_trip_sheet" not in _offered("affiliate_offer", "affiliate")


def test_the_quote_and_pay_links_must_stay_in_their_messages():
    row = listing.row_data(_row("send_pay_link")) | {"sms_body": "Pay us", "email_body": "Pay us"}
    assert "pay_link" in fields.validate(row)["sms_body"]


def test_the_pay_and_terms_links_email_by_podium_with_no_subject():
    for key in ("send_pay_link", "send_terms_link"):
        assert triggers.get(key).email_via == "podium"
        assert defaults.CLICKS[key].subject == ""
    # Any other email still needs one.
    data = listing.row_data(_row("send_quote")) | {"subject": ""}
    assert fields.validate(data) == {"subject": "Add a subject for the email."}


def test_the_editor_previews_a_click_row_against_an_order():
    lead = _quotable()
    for key in CLICK_KEYS:
        shown = editor.preview(_row(key), lead)
        assert shown["email"] or shown["sms"], key


# --- the sender ----------------------------------------------------------------------------


def _person(**over):
    values = {
        "name": "Dana Whitfield",
        "label": "Dana Whitfield (booking contact)",
        "phone": "+12025550101",
        "email": "dana@example.com",
        "contact_id": None,
    }
    return Recipient(**(values | over))


def test_a_click_records_any_provider_error_and_carries_on():
    lead, _ = _booked()
    extra = {"pay_link": "https://x.test/pay/", "payment_for": "reservation Q-1"}
    with patch(SEND, side_effect=[RuntimeError("podium down"), {"uid": "e1"}]):
        result = sender.send_now("send_pay_link", lead, to=[_person()], extra_ctx=extra)

    assert result.sent is True
    assert [(d.channel, d.status, d.reason) for d in result.deliveries] == [
        ("sms", "failed", "podium down"),
        ("email", "sent", ""),
    ]


def test_a_provider_answer_that_is_not_a_dict_records_no_id():
    lead, _ = _booked()
    extra = {"pay_link": "https://x.test/pay/", "payment_for": "reservation Q-1"}
    with patch(SEND):  # a bare MagicMock answer
        result = sender.send_now(
            "send_pay_link", lead, to=[_person()], extra_ctx=extra, channels={"sms"}
        )

    assert result.sent is True
    assert result.deliveries[0].provider_id == ""


def test_podium_text_is_the_text_the_click_sends():
    lead, _ = _booked()
    extra = {"pay_link": "https://x.test/pay/", "payment_for": "trip APC-600001"}
    shown = sender.podium_text("send_pay_link", lead, _person(), "sms", extra_ctx=extra)
    with patch(SEND, return_value={"uid": "s1"}) as send:
        sender.send_now("send_pay_link", lead, to=[_person()], extra_ctx=extra, channels={"sms"})

    assert send.call_args.kwargs["body"] == shown
    assert shown == "Here's a secure link to pay for trip APC-600001: https://x.test/pay/"


# --- each click sends its row and leaves the trail ------------------------------------------


def _trail(key: str) -> TouchPoint:
    tp = TouchPoint.objects.get(kind=key)
    assert tp.message == _row(key)
    assert tp.status == TouchPoint.Status.SENT
    return tp


def test_send_quote_sends_its_row():
    lead = _quotable()
    with patch(SEND, return_value={"uid": "q1"}):
        assert lead_services.send_quote(lead, base_url=BASE_URL).ok

    tp = _trail("send_quote")
    assert sorted(tp.deliveries.values_list("channel", "address")) == [
        ("email", "dana@example.com"),
        ("sms", "+12025550101"),
    ]
    cids = [a["Content-ID"] for a in mail.outbox[0].attachments if hasattr(a, "get")]
    assert "<logo>" in cids


def test_an_account_quote_sends_the_account_row():
    from apps.billing.factories import AccountGroupFactory

    lead = _quotable()
    lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
    lead.save(update_fields=["billing_group"])
    with patch(SEND, return_value={"uid": "q2"}):
        assert lead_services.send_quote(lead, base_url=BASE_URL).ok

    _trail("send_quote_on_account")
    assert not TouchPoint.objects.filter(kind="send_quote").exists()


def test_the_rows_wording_is_what_goes_out():
    MessageTemplate.objects.filter(key="send_pay_link").update(sms_body="Pay here: {pay_link}")
    lead, _ = _booked()
    with patch(SEND, return_value={"uid": "p1"}) as send:
        payments.send_pay_link(lead, base_url=BASE_URL, sender_name="Sam")

    assert send.call_args.kwargs["body"].startswith("Pay here: https://portal.example.com/quote/")
    message = lead.contact.conversation.messages.get()
    assert message.body == send.call_args.kwargs["body"]
    assert message.podium_message_uid == "p1"
    _trail("send_pay_link")


def test_send_terms_sends_its_row():
    lead, _ = _booked(phone="")
    with patch(SEND, return_value={"uid": "t1"}) as send:
        assert payments.send_terms_link(lead, base_url=BASE_URL, sender_name="Sam") == "email"

    assert send.call_args.kwargs["channel_type"] == "email"
    assert list(_trail("send_terms_link").deliveries.values_list("channel", flat=True)) == ["email"]


def test_a_failed_link_send_is_a_podium_error_and_records_nothing():
    from apps.integrations.podium import PodiumAPIError

    lead, _ = _booked()
    with patch(SEND, side_effect=PodiumAPIError("500 boom")), pytest.raises(PodiumAPIError):
        payments.send_pay_link(lead, base_url=BASE_URL, sender_name="Sam")

    assert not Message.objects.filter(conversation__contact=lead.contact).exists()


def _receipt(lead, trip) -> Charge:
    return lead.payment.charges.create(
        kind=Charge.Kind.TRIP,
        reservation=trip,
        amount=Decimal("20.00"),
        status=Charge.Status.SUCCEEDED,
        idempotency_key=f"r-{trip.pk}",
        receipt_due_at=timezone.now(),
        receipt_lines=[["Parking", "20.00"]],
    )


def test_a_receipt_sends_its_row_and_stamps_the_charge():
    lead, trip = _booked()
    charge = _receipt(lead, trip)

    assert receipts.send_receipt(charge.pk) is True

    charge.refresh_from_db()
    assert charge.receipt_sent_at is not None
    tp = _trail("payment_receipt")
    assert tp.reservation == trip
    assert list(tp.deliveries.values_list("address", flat=True)) == ["dana@example.com"]
    assert "Parking" in mail.outbox[0].body


def test_a_receipt_tried_moments_ago_is_left_to_that_send():
    """The receipt goes out after its row lock is released: a second call while the first
    is sending must not send it again."""
    lead, trip = _booked()
    charge = _receipt(lead, trip)
    Charge.objects.filter(pk=charge.pk).update(receipt_attempted_at=timezone.now())

    assert receipts.send_receipt(charge.pk) is False
    assert mail.outbox == []
    assert not TouchPoint.objects.filter(kind="payment_receipt").exists()


def test_the_offer_sends_its_row_with_the_logo():
    _, trip = _booked()
    vendor = VendorFactory(name="Capital Chauffeurs", email="ops@capital.example")
    dispatch_services.send_offer(trip, vendor, payout=Decimal("215.00"))

    tp = _trail("affiliate_offer")
    assert tp.reservation == trip
    assert list(tp.deliveries.values_list("address", flat=True)) == ["ops@capital.example"]
    cids = [a["Content-ID"] for a in mail.outbox[0].attachments if hasattr(a, "get")]
    assert "<logo>" in cids


def test_the_cancellation_sends_its_row_without_a_logo():
    _, trip = _booked()
    assignment = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Reston Coach Co", email="ops@reston.example"),
        status=Assignment.Status.CONFIRMED,
    )
    assert dispatch_services.send_cancellation(assignment) is True

    _trail("affiliate_cancel")
    assert not [a for a in mail.outbox[0].attachments if hasattr(a, "get")]
    assert Delivery.objects.filter(message=_row("affiliate_cancel")).count() == 1


# --- the Messages page -------------------------------------------------------------------


def test_the_messages_page_lists_the_clicks_with_no_switch(client):
    from django.urls import reverse
    from django.utils.html import escape

    from apps.accounts.factories import UserFactory
    from apps.accounts.models import User

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    html = client.get(reverse("messages")).content.decode()

    for key in CLICK_KEYS:
        row = _row(key)
        assert escape(row.name) in html, key
        assert reverse("message_edit", args=[key]) in html, key
        assert reverse("message_toggle", args=[key]) not in html, key
        assert client.get(reverse("message_edit", args=[key])).status_code == 200, key
        assert client.post(reverse("message_toggle", args=[key])).status_code == 400, key
    for label in ("Send quote", "Send payment link", "Send terms link", "Payment receipt"):
        assert f"Sent when staff click {label}" in html
    assert "needs attention" not in html


# --- fix round 1: the button sets who, how and when -------------------------------------


def _owner(client):
    from apps.accounts.factories import UserFactory
    from apps.accounts.models import User

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


def _edit(client, key, **over):
    from django.urls import reverse

    from apps.settings.tests.test_message_editor import post_data

    return client.post(reverse("message_edit", args=[key]), post_data(_row(key), **over))


@pytest.mark.parametrize(
    "over",
    [
        {"send_sms": None},
        {"send_email": None},
        {"recipients": ["billing_contact"]},
        {"trigger": "quote_sent"},
        {"audience": "planner", "recipients": ["planner"]},
    ],
    ids=["sms-off", "email-off", "recipients", "trigger", "audience"],
)
def test_the_button_fields_of_a_click_row_cannot_be_changed(client, over):
    _owner(client)
    before = listing.row_data(_row("send_pay_link"))

    resp = _edit(client, "send_pay_link", **over)

    assert resp.status_code == 200  # the form comes back with the reason
    assert escape(editor.SET_BY_BUTTON) in resp.content.decode()
    assert listing.row_data(_row("send_pay_link")) == before


def test_service_types_on_a_click_row_cannot_be_changed(client):
    from apps.leads.factories import ServiceTypeFactory

    _owner(client)
    resp = _edit(client, "payment_receipt", service_types=[ServiceTypeFactory().pk])

    assert resp.status_code == 200
    assert not _row("payment_receipt").service_types.exists()


def test_a_click_rows_wording_subject_and_name_stay_editable(client):
    _owner(client)
    resp = _edit(client, "send_quote", name="Quote email", subject="Quote {quote_no} is ready")

    assert resp.status_code == 302
    row = _row("send_quote")
    assert (row.name, row.subject) == ("Quote email", "Quote {quote_no} is ready")


def test_an_sms_off_attempt_leaves_the_pay_link_routing_alone(client):
    _owner(client)
    _edit(client, "send_pay_link", send_sms=None)
    by_text, _ = _booked()
    by_email, _ = _booked(phone="", email="pat@example.com")

    with patch(SEND, return_value={"uid": "p"}) as send:
        assert payments.send_pay_link(by_text, base_url=BASE_URL, sender_name="Sam") == "sms"
        assert payments.send_pay_link(by_email, base_url=BASE_URL, sender_name="Sam") == "email"
    assert [c.kwargs["channel_type"] for c in send.call_args_list] == ["phone", "email"]


def test_the_editor_shows_who_the_button_sends_to_not_checkboxes(client):
    from django.urls import reverse

    _owner(client)
    html = client.get(reverse("message_edit", args=["send_pay_link"])).content.decode()

    assert escape(editor.CLICK_WHO["send_pay_link"]) in html
    assert 'type="checkbox" name="recipients"' not in html
    assert 'name="trigger" value="send_pay_link"' in html  # posted as is, not picked
    assert 'id="id_trigger"' not in html
    assert '@click="sms = !sms' not in html


def test_the_preview_says_who_the_button_sends_to():
    lead = _quotable()
    assert editor.preview(_row("send_pay_link"), lead)["to"] == [editor.CLICK_WHO["send_pay_link"]]


def test_every_click_row_says_who_the_button_sends_to():
    assert set(editor.CLICK_WHO) == set(CLICK_KEYS)


@pytest.mark.parametrize("trigger", ["send_quote", "deposit_report"])
def test_an_added_row_cannot_pick_a_click_or_schedule_trigger(client, trigger):
    from django.urls import reverse

    from apps.messaging.factories import MessageRowFactory
    from apps.settings.tests.test_message_editor import post_data

    _owner(client)
    blank = MessageRowFactory.build(
        trigger=trigger,
        recipients=["booking_contact"],
        send_sms=True,
        sms_body="Hi {first_name}",
        audience="staff" if trigger == "deposit_report" else "customer",
        staff_emails=["ops@apc.test"],
    )
    count = MessageTemplate.objects.count()

    resp = client.post(reverse("messages_new"), post_data(blank, timing=(0, "minutes")))

    assert resp.status_code == 200
    assert escape(editor.NOT_ADDABLE) in resp.content.decode()
    assert MessageTemplate.objects.count() == count


def test_the_trigger_choices_for_an_added_row_leave_out_clicks_and_schedules(client):
    from django.urls import reverse

    _owner(client)
    html = client.get(reverse("messages_new")).content.decode()
    assert "Staff clicks: Send quote" not in html
    assert "Schedules: Deposit report" not in html
    assert "Events: Quote sent" in html


# --- fix round 1: blocks inside the best-effort net, preheaders, the stamp ----------------


def test_a_failing_block_never_breaks_send_quote():
    lead = _quotable()
    with (
        patch("apps.messaging.rendering.block", side_effect=RuntimeError("bad block")),
        patch(SEND, return_value={"uid": "q"}),
    ):
        result = lead_services.send_quote(lead, base_url=BASE_URL)

    assert result.ok
    assert result.delivery["email"] == {
        "sent": False,
        "recipient": "dana@example.com",
        "error": "bad block",
    }
    assert result.delivery["sms"]["sent"] is False


def test_a_failing_block_keeps_the_offer_and_raises_the_alert():
    from apps.notifications.models import Notification

    _, trip = _booked()
    vendor = VendorFactory(email="ops@capital.example")
    with patch("apps.messaging.rendering.block", side_effect=RuntimeError("bad block")):
        assignment = dispatch_services.send_offer(trip, vendor, payout=Decimal("215.00"))

    assert assignment.status == Assignment.Status.OFFERED
    assert Notification.objects.filter(lead=trip.lead).exists()


def test_a_failing_block_makes_the_cancellation_a_no():
    _, trip = _booked()
    assignment = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(email="ops@reston.example"),
        status=Assignment.Status.CONFIRMED,
    )
    with patch("apps.messaging.rendering.block", side_effect=RuntimeError("bad block")):
        assert dispatch_services.send_cancellation(assignment) is False


def _preheader(html: str) -> str:
    import re

    return re.search(r'<div style="display:none;[^"]*">(.*?)</div>', html, re.S).group(1)


def _todays(template: str, context: dict) -> str:
    from django.template.loader import render_to_string

    return _preheader(render_to_string(f"email/{template}.html", context))


def test_each_email_keeps_todays_preheader():
    lead = _quotable()
    with patch(SEND, return_value={"uid": "q"}):
        lead_services.send_quote(lead, base_url=BASE_URL)
    lead.refresh_from_db()
    quote = _todays(
        "quote_sent", lead_services._quote_email_context(lead, lead.payment, "https://x.test")
    )
    assert quote.startswith(f"Quote {lead.quote_no} — $185.00 total, 50% deposit")
    assert _preheader(mail.outbox[-1].alternatives[0][0]) == quote

    booked, trip = _booked(email="pat@example.com")
    charge = _receipt(booked, trip)
    receipts.send_receipt(charge.pk)
    receipt = _todays("trip_receipt", receipts.receipt_context(charge))
    assert receipt == f"$20.00 for your trip · Receipt R-{charge.pk}"
    assert _preheader(mail.outbox[-1].alternatives[0][0]) == receipt

    vendor = VendorFactory(email="ops@capital.example")
    offer = dispatch_services.send_offer(trip, vendor, payout=Decimal("215.00"))
    expected = _todays("vendor_offer", dispatch_services.offer_email_context(offer))
    assert expected.endswith("— $215.00 payout.")
    assert _preheader(mail.outbox[-1].alternatives[0][0]) == expected

    assert dispatch_services.send_cancellation(offer) is True
    cancel = _todays(
        "vendor_cancel", {"vendor": vendor, "trip": trip, "stops": list(trip.ordered_stops)}
    )
    assert "released this trip" in cancel
    assert _preheader(mail.outbox[-1].alternatives[0][0]) == cancel


def test_the_receipt_stamp_never_overwrites_an_earlier_send():
    lead, trip = _booked()
    charge = _receipt(lead, trip)
    earlier = timezone.now() - timedelta(minutes=1)

    def sent_meanwhile(*args, **kwargs):
        Charge.objects.filter(pk=charge.pk).update(
            receipt_sent_at=earlier, receipt_message_id="first"
        )
        return sender.SendResult(sent=True, deliveries=[], errors=[])

    with patch.object(sender, "send_now", side_effect=sent_meanwhile):
        receipts.send_receipt(charge.pk)

    charge.refresh_from_db()
    assert (charge.receipt_sent_at, charge.receipt_message_id) == (earlier, "first")
