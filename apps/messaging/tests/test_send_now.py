"""`sender.send_now`: a staff click, invite or report sent inside the request through the
shared sender (spec C sections 3 and 8), leaving a TouchPoint and Delivery trail.

Gating is today's button sends': `OUTBOUND_ENABLED` (staff mail is `internal` and goes
regardless), never `TOUCHPOINTS_ENABLED` or the master switch. Podium and Postmark are always
mocked (Postmark through `deliver_html_email`, or Django's test mail outbox).
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.core import outbound
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import rules, sender
from apps.messaging.factories import MessageRowFactory
from apps.messaging.models import Delivery, MessageTemplate, NotificationConfig, TouchPoint
from apps.messaging.recipients import Recipient
from apps.messaging.rendering import Block

pytestmark = pytest.mark.django_db

SEND = "apps.integrations.podium.send_message"
POSTMARK = "apps.notifications.email.deliver_html_email"
_A = MessageTemplate.Audience


@pytest.fixture(autouse=True)
def live(settings):
    settings.OUTBOUND_ENABLED = True
    settings.TOUCHPOINTS_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"


def _lead(**kw):
    contact = ContactFactory(name="Dana Whit", phone="+12025550100", email="dana@example.com")
    return LeadFactory(contact=contact, **kw)


def _click(key="click-quote", **kw):
    kw.setdefault("trigger", "send_quote")
    kw.setdefault("audience", _A.CUSTOMER)
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("subject", "Your quote {quote_no}")
    kw.setdefault("email_body", "Hi {first_name},\n\n{quote_summary}\n\nSee {quote_link}")
    kw.setdefault("sms_body", "Hi {first_name}, quote {quote_no}: {quote_link}")
    return MessageRowFactory(key=key, **kw)


def _planner_row(**kw):
    return _click(
        key="click-invite",
        trigger="planner_invite",
        audience=_A.PLANNER,
        recipients=[],
        send_sms=False,
        subject="Your account with {company_name}",
        email_body="Hi {first_name},\n\n{button}",
        sms_body="",
        **kw,
    )


def _staff_row(**kw):
    kw.setdefault("staff_emails", ["ops@apc.test"])
    return _click(
        key="click-report",
        trigger="deposit_report",
        audience=_A.STAFF,
        recipients=[],
        send_sms=False,
        subject="Unpaid deposits",
        email_body="Unpaid deposits\n\n{report_table}",
        sms_body="",
        **kw,
    )


def _planner(email="wendy@plans.test", name="Wendy Planner"):
    return Recipient(
        name=name,
        label=f"{name} (planner)",
        phone="",
        email=email,
        contact_id=None,
        channels=("email",),
    )


SUMMARY = Block(text="  QUOTE NO.  Q-1", html="<table>summary</table>")
QUOTE_LINK = {"quote_link": "https://request.example.com/quote/abc/"}
# What a Send-quote click hands over: its link and the generated summary.
ARGS = {"extra_ctx": QUOTE_LINK, "blocks": {"quote_summary": SUMMARY}}


# --- the trail -------------------------------------------------------------------------


def test_sends_now_and_leaves_a_touchpoint_and_deliveries():
    lead = _lead()
    row = _click()

    with (
        patch(SEND, return_value={"uid": "pod-1"}) as send,
        patch(POSTMARK, return_value="pm-1") as mail_,
    ):
        result = sender.send_now(
            row.key, lead, extra_ctx=QUOTE_LINK, blocks={"quote_summary": SUMMARY}
        )

    assert result.sent is True
    assert result.errors == []
    tp = TouchPoint.objects.get(message=row)
    assert (tp.lead, tp.reservation, tp.kind, tp.status) == (
        lead,
        None,
        row.key,
        TouchPoint.Status.SENT,
    )
    assert tp.sent_at is not None
    assert sorted((d.channel, d.address, d.status) for d in result.deliveries) == [
        ("email", "dana@example.com", "sent"),
        ("sms", "+12025550100", "sent"),
    ]
    assert set(result.deliveries) == set(Delivery.objects.filter(touchpoint=tp))
    # The text carries the block's text and the caller's link; the email is the layout.
    assert send.call_args.kwargs["body"] == (
        f"Hi Dana, quote {lead.quote_no}: https://request.example.com/quote/abc/"
    )
    kwargs = mail_.call_args.kwargs
    assert (kwargs["to"], kwargs["subject"], kwargs["template"], kwargs["internal"]) == (
        "dana@example.com",
        f"Your quote {lead.quote_no}",
        "message",
        False,
    )
    assert kwargs["context"]["body"] == (
        "Hi Dana,\n\n  QUOTE NO.  Q-1\n\nSee https://request.example.com/quote/abc/"
    )
    assert "<table>summary</table>" in kwargs["context"]["body_html"]


def test_a_second_click_is_a_second_send():
    lead = _lead()
    row = _click(send_sms=False)

    with patch(POSTMARK, return_value="pm"):
        sender.send_now(row.key, lead, **ARGS)
        sender.send_now(row.key, lead, **ARGS)

    assert TouchPoint.objects.filter(message=row, status=TouchPoint.Status.SENT).count() == 2


def test_the_cron_never_resends_a_click():
    lead = _lead()
    row = _click(send_sms=False)
    with patch(POSTMARK, return_value="pm"):
        sender.send_now(row.key, lead, **ARGS)

    with patch(POSTMARK) as again, patch(SEND) as text:
        assert sender.run_touchpoints() == 0

    again.assert_not_called()
    text.assert_not_called()


def test_channels_narrow_the_rows_channels():
    lead = _lead()
    row = _click()

    with patch(SEND, return_value={"uid": "pod-1"}) as send, patch(POSTMARK) as mail_:
        result = sender.send_now(row.key, lead, **ARGS, channels={"sms"})

    assert result.sent is True
    mail_.assert_not_called()
    assert send.call_count == 1
    assert [d.channel for d in result.deliveries] == ["sms"]


def test_to_replaces_the_rows_recipients():
    lead = _lead()
    row = _click(send_sms=False)
    other = Recipient(
        name="Bea Other", label="Bea Other (account)", phone="", email="bea@x.test", contact_id=None
    )

    with patch(POSTMARK, return_value="pm") as mail_:
        sender.send_now(row.key, lead, to=[other], **ARGS)

    assert mail_.call_args.kwargs["to"] == "bea@x.test"
    # A customer message greets the person who gets it.
    assert mail_.call_args.kwargs["context"]["body"].startswith("Hi Bea,")


def test_nobody_to_send_to_is_skipped_and_said():
    lead = LeadFactory(contact=ContactFactory(phone="", email=""))
    row = _click(send_sms=False)

    with patch(POSTMARK) as mail_:
        result = sender.send_now(row.key, lead, **ARGS)

    mail_.assert_not_called()
    assert result.sent is False
    tp = TouchPoint.objects.get(message=row)
    assert tp.status == TouchPoint.Status.SKIPPED
    assert result.errors == [tp.error]
    assert [(d.status, d.reason) for d in result.deliveries] == [("skipped", "No email on file")]


# --- no order: invites -----------------------------------------------------------------


def test_an_invite_needs_no_order():
    row = _planner_row()
    button = Block(text="Create your account:\nhttps://x/r", html='<a href="https://x/r">Go</a>')

    with patch(POSTMARK, return_value="pm-9") as mail_:
        result = sender.send_now(row.key, to=[_planner()], blocks={"button": button})

    assert result.sent is True
    tp = TouchPoint.objects.get(message=row)
    assert tp.lead_id is None
    assert tp.status == TouchPoint.Status.SENT
    kwargs = mail_.call_args.kwargs
    assert kwargs["subject"] == "Your account with All Pro Charter"
    assert kwargs["context"]["body"] == "Hi Wendy,\n\nCreate your account:\nhttps://x/r"
    assert '<a href="https://x/r">Go</a>' in kwargs["context"]["body_html"]


def test_an_invite_with_nobody_to_send_to_is_skipped():
    row = _planner_row()

    with patch(POSTMARK) as mail_:
        result = sender.send_now(row.key)

    mail_.assert_not_called()
    assert result.sent is False
    assert TouchPoint.objects.get(message=row).error == sender.NO_RECIPIENT


def test_a_name_with_a_script_is_escaped_in_the_html_part():
    """End to end through our own mail (Django's test outbox stands in for Postmark)."""
    row = _planner_row()

    sender.send_now(
        row.key,
        to=[_planner(name="<script>alert(1)</script>")],
        blocks={"button": Block(text="go", html="<b>go</b>")},
    )

    [message] = mail.outbox
    html = message.alternatives[0][0]
    assert "<script>alert(1)</script>" not in html
    assert "Hi &lt;script&gt;alert(1)&lt;/script&gt;," in html
    assert "<b>go</b>" in html
    assert message.body == "Hi <script>alert(1)</script>,\n\ngo"


def test_staff_mail_goes_internal():
    row = _staff_row()
    table = Block(text="  Q-1", html="<table>q</table>")

    with patch(POSTMARK, return_value="pm") as mail_:
        result = sender.send_now(row.key, blocks={"report_table": table})

    assert result.sent is True
    assert mail_.call_args.kwargs["internal"] is True
    assert mail_.call_args.kwargs["to"] == "ops@apc.test"
    assert TouchPoint.objects.get(message=row).lead_id is None


# --- gating: as today's button sends --------------------------------------------------


def test_outbound_off_refuses_customer_sends(settings):
    settings.OUTBOUND_ENABLED = False
    lead = _lead()
    row = _click()

    with patch(SEND) as send, patch(POSTMARK) as mail_:
        result = sender.send_now(row.key, lead, **ARGS)

    send.assert_not_called()
    mail_.assert_not_called()
    assert result.sent is False
    assert result.errors == [outbound.OFF_MESSAGE]
    tp = TouchPoint.objects.get(message=row)
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, sender.OUTBOUND_OFF)


def test_outbound_off_still_sends_staff_mail(settings):
    settings.OUTBOUND_ENABLED = False
    row = _staff_row()

    with patch(POSTMARK, return_value="pm") as mail_:
        result = sender.send_now(row.key, blocks={"report_table": Block("t", "<p>t</p>")})

    assert result.sent is True
    assert mail_.call_args.kwargs["internal"] is True


def test_touchpoints_switch_does_not_gate_a_click(settings):
    settings.TOUCHPOINTS_ENABLED = False
    lead = _lead()
    row = _click(send_sms=False)

    with patch(POSTMARK, return_value="pm"):
        assert sender.send_now(row.key, lead, **ARGS).sent is True


def test_master_switch_does_not_gate_a_click():
    config = NotificationConfig.load()
    config.enabled = False
    config.save()
    lead = _lead()
    row = _click(send_sms=False)

    with patch(POSTMARK, return_value="pm"):
        assert sender.send_now(row.key, lead, **ARGS).sent is True


def test_a_row_switched_off_sends_nothing():
    lead = _lead()
    row = _click(enabled=False)

    with patch(SEND) as send, patch(POSTMARK) as mail_:
        result = sender.send_now(row.key, lead, **ARGS)

    send.assert_not_called()
    mail_.assert_not_called()
    assert result.sent is False
    assert result.errors == [rules.DISABLED]
    assert TouchPoint.objects.get(message=row).status == TouchPoint.Status.SKIPPED


def test_a_click_keeps_its_callers_checks_not_the_order_rules():
    """A refund receipt goes out on a cancelled (lost) order today; the caller decides what
    may be sent, as it does now, not the automated messages' order rules."""
    lead = _lead(status=Lead.Status.LOST)
    row = _click(trigger="payment_receipt", send_sms=False)

    with patch(POSTMARK, return_value="pm"):
        assert sender.send_now(row.key, lead, **ARGS).sent is True


def test_a_click_does_not_wait_for_public_base_url(settings):
    """The caller hands over its links (built from the request), so a blank PUBLIC_BASE_URL
    does not hold the click the way it holds a queued send."""
    settings.PUBLIC_BASE_URL = ""
    lead = _lead()
    row = _click(send_sms=False)

    with patch(POSTMARK, return_value="pm") as mail_:
        result = sender.send_now(row.key, lead, **ARGS)

    assert result.sent is True
    assert "https://request.example.com/quote/abc/" in mail_.call_args.kwargs["context"]["body"]


# --- failures ---------------------------------------------------------------------------


def test_postmark_error_text_lands_on_the_delivery():
    lead = _lead()
    row = _click(send_sms=False)

    with patch(
        "apps.notifications.email.EmailMultiAlternatives.send",
        side_effect=OSError("422 Inactive recipient"),
    ):
        result = sender.send_now(row.key, lead, **ARGS)

    assert result.sent is False
    [delivery] = result.deliveries
    assert (delivery.status, delivery.reason) == ("failed", "422 Inactive recipient")
    assert result.errors == ["422 Inactive recipient"]
    tp = TouchPoint.objects.get(message=row)
    assert (tp.status, tp.error) == (TouchPoint.Status.FAILED, "email: 422 Inactive recipient")


def test_an_unexpected_error_never_breaks_the_click():
    lead = _lead()
    row = _click(send_sms=False, email_body="Hi {no_such_field}")

    with patch(POSTMARK) as mail_:
        result = sender.send_now(row.key, lead)

    mail_.assert_not_called()
    assert result.sent is False
    tp = TouchPoint.objects.get(message=row)
    assert tp.status == TouchPoint.Status.FAILED
    assert result.errors == ["unexpected error"]


# --- rules.check without an order -------------------------------------------------------


def test_check_without_a_lead_skips_the_order_rules():
    row = _staff_row()
    tp = TouchPoint(lead=None, message=row, kind=row.key, scheduled_for=timezone.now())

    assert rules.check(tp, row, master=True) == rules.SEND


def test_check_without_a_lead_still_honours_the_rows_switch():
    row = _staff_row(enabled=False)
    tp = TouchPoint(lead=None, message=row, kind=row.key, scheduled_for=timezone.now())

    assert rules.check(tp, row, master=True) == ("skip", rules.DISABLED)


def test_an_interrupted_click_is_closed_by_the_cron_not_sent():
    """A click whose process died mid-send leaves its TouchPoint SCHEDULED; once the lease
    runs out the cron must not send it without the click's recipients, values and blocks."""
    lead = _lead()
    row = _click()
    tp = TouchPoint.objects.create(
        lead=lead,
        message=row,
        kind=row.key,
        scheduled_for=timezone.now() - timedelta(minutes=10),
    )

    with patch(SEND) as send, patch(POSTMARK) as mail_:
        assert sender.run_touchpoints() == 0

    send.assert_not_called()
    mail_.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, sender.INTERRUPTED)


def test_a_click_is_leased_from_birth():
    """Between its creation and its send, the cron's claim can't take a click's TouchPoint."""
    lead = _lead()
    row = _click(send_sms=False)
    seen = {}

    def postmark(**kwargs):
        tp = TouchPoint.objects.get(message=row)
        seen["leased"] = tp.updated_at > timezone.now()
        return "pm"

    with patch(POSTMARK, side_effect=postmark):
        sender.send_now(row.key, lead, **ARGS)

    assert seen == {"leased": True}
