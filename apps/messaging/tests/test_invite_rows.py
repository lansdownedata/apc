"""The invites, the planner password reset and the wedding-details email as message rows
(spec C section 8, APC-120).

Each sends a seeded row through `sender.send_now`: the wording comes from the row, the link
button and the wedding answers from their callers as blocks, and the person from the caller.
What each sends word for word is held by `test_invites_golden.py`; this file holds the rows,
their locks and the trail. Invites and password resets can't be switched off.
"""

import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.template import loader
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from apps.accounts import services as account_services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import defaults, editor, fields, listing, sender, triggers
from apps.messaging.models import Delivery, MessageTemplate, NotificationConfig, TouchPoint
from apps.planners import services as planner_services
from apps.planners.models import PlannerAccount
from apps.planners.tokens import planner_reset_token, planner_token
from apps.public import services as public_services
from apps.settings.tests.test_message_editor import post_data

pytestmark = pytest.mark.django_db

BASE_URL = "https://portal.example.com"
DELIVER = "apps.notifications.email.deliver_html_email"
INVITE_KEYS = ("planner_invite", "planner_password_reset", "user_invite")
KEYS = (*INVITE_KEYS, "wedding_details_received")
PASSWORD = "a-long-pass-phrase-77"


@pytest.fixture(autouse=True)
def live(settings):
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = BASE_URL
    settings.COMPANY_PHONE = "(703) 555-0100"


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


def _edit(client, key, **over):
    return client.post(reverse("message_edit", args=[key]), post_data(_row(key), **over))


def _invite_planner(name="Dana Planner", email="dana@example.com") -> PlannerAccount:
    contact = ContactFactory(name=name, email=email, phone="")
    account, sent = planner_services.invite(contact, actor=UserFactory())
    assert sent is True
    return account


def _registered() -> PlannerAccount:
    return planner_services.register(_invite_planner(), PASSWORD)


def _invite_user(**over):
    values = {
        "first_name": "New",
        "last_name": "Hire",
        "email": "new.hire@allprocharter.com",
        "role": User.Role.AGENT,
        "can_manage_payments": False,
        "actor": UserFactory(role=User.Role.OWNER_ADMIN),
    }
    return account_services.invite_user(**(values | over))


def _wedding_lead(**lead) -> Lead:
    contact = ContactFactory(name="Jane Rider", email="jane@example.com", phone="")
    return LeadFactory(contact=contact, intake_payload={}, **lead)


def _link(body: str, page: str) -> str:
    return re.search(re.escape(BASE_URL) + page + r"\S+", body).group(0)


# --- the seeded rows -------------------------------------------------------------------


def test_the_migration_seeded_every_invite_row_from_its_original():
    for key in KEYS:
        row, d = _row(key), defaults.ORIGINALS[key]
        assert row.seeded is True and row.enabled is True, key
        assert (row.trigger, row.audience, row.section, row.name) == (
            d.trigger,
            d.audience,
            d.section,
            d.name,
        ), key
        assert (row.subject, row.email_body, row.sms_body) == (d.subject, d.email_body, d.sms_body)
        assert (row.send_sms, row.send_email) == (False, True), key
        assert row.recipients == list(d.recipients), key
        assert row.timing_amount == 0, key
        assert not listing.edited(row), key


def test_the_invite_originals_sit_beside_the_others():
    assert set(defaults.INVITES) == set(KEYS)
    assert not set(defaults.INVITES) & (set(defaults.DEFAULTS) | set(defaults.CLICKS))
    assert defaults.ORIGINALS == {
        **defaults.DEFAULTS,
        **defaults.CLICKS,
        **defaults.INVITES,
        **defaults.REPORTS,
    }


def test_each_row_sends_on_its_own_trigger_to_its_audience():
    got = {k: (defaults.INVITES[k].trigger, defaults.INVITES[k].audience) for k in KEYS}
    assert got == {
        "planner_invite": ("planner_invite", "planner"),
        "planner_password_reset": ("planner_password_reset", "planner"),
        "user_invite": ("user_invite", "staff"),
        "wedding_details_received": ("wedding_details_received", "customer"),
    }


def test_only_the_wedding_details_trigger_is_sent_by_its_caller():
    assert {k for k, t in triggers.CATALOG.items() if t.caller_sends} == {
        "wedding_details_received"
    }


@pytest.mark.parametrize("key", KEYS)
def test_an_invite_row_has_no_problem_and_validates(key):
    row = _row(key)
    assert listing.problems(row) == []
    assert listing.problems(row, master_on=False) == []
    assert fields.validate(listing.row_data(row)) == {}


@pytest.mark.parametrize("key", KEYS)
def test_reset_puts_an_invite_row_back(key):
    row = _row(key)
    row.email_body = "Edited {company_name}"
    row.save()
    editor.reset(row)
    row.refresh_from_db()
    assert row.email_body == defaults.INVITES[key].email_body
    assert row.enabled is True


def test_reseeding_never_touches_an_invite_row():
    MessageTemplate.objects.filter(key="user_invite").update(subject="Edited {company_name}")
    defaults.seed(keys=KEYS)
    assert _row("user_invite").subject == "Edited {company_name}"


def test_the_page_says_who_an_invite_goes_to():
    assert "The account holder" in listing.summary(_row("user_invite"))
    assert "The account holder" in listing.summary(_row("planner_invite"))


# --- fields ------------------------------------------------------------------------------


def _offered(trigger, audience):
    return {f.key for f in fields.fields_for(trigger, audience)}


def test_each_trigger_offers_what_its_caller_fills():
    for key in ("planner_invite", "planner_password_reset"):
        assert {"account_name", "account_button"} <= _offered(key, "planner")
        assert "role_label" not in _offered(key, "planner")
    assert {"account_first_name", "account_button", "role_label"} <= _offered(
        "user_invite", "staff"
    )
    assert {"form_name", "wedding_details", "resume_button", "quote_no"} <= _offered(
        "wedding_details_received", "customer"
    )
    for trigger in ("quote_sent", "lead_created", "send_quote"):
        assert not _offered(trigger, "customer") & {
            "account_button",
            "role_label",
            "wedding_details",
            "resume_button",
        }


@pytest.mark.parametrize(
    ("key", "link"),
    [
        ("planner_invite", "account_button"),
        ("planner_password_reset", "account_button"),
        ("user_invite", "account_button"),
        ("wedding_details_received", "resume_button"),
    ],
)
def test_the_link_must_stay_in_the_message(key, link):
    data = listing.row_data(_row(key))
    data["email_body"] = data["email_body"].replace("{" + link + "}", "")
    assert f"{{{link}}}" in fields.validate(data)["email_body"]


# --- can't be switched off; who, how and when are the caller's ---------------------------


def test_the_page_shows_no_switch_for_an_invite_and_a_switch_for_the_wedding_email(client):
    _owner(client)
    html = client.get(reverse("messages")).content.decode()
    for key in INVITE_KEYS:
        assert reverse("message_edit", args=[key]) in html, key
        assert reverse("message_toggle", args=[key]) not in html, key
    assert reverse("message_toggle", args=["wedding_details_received"]) in html
    assert "needs attention" not in html


@pytest.mark.parametrize("key", INVITE_KEYS)
def test_an_invite_cannot_be_toggled_off(client, key):
    _owner(client)
    assert client.post(reverse("message_toggle", args=[key])).status_code == 400
    assert _row(key).enabled is True


@pytest.mark.parametrize("key", INVITE_KEYS)
def test_the_editor_cannot_switch_an_invite_off(client, key):
    _owner(client)
    resp = _edit(client, key, enabled=None)
    assert resp.status_code == 302
    assert _row(key).enabled is True


def test_the_wedding_email_can_be_switched_off_and_then_sends_nothing(client):
    _owner(client)
    assert client.post(reverse("message_toggle", args=["wedding_details_received"])).json() == {
        "ok": True,
        "enabled": False,
    }
    lead = _wedding_lead()
    assert public_services.send_wedding_confirmation(lead, base_url=BASE_URL) is False
    assert mail.outbox == []


# Someone the row could otherwise go to, per audience.
_OTHER_PEOPLE = {
    "planner_invite": {"recipients": ["planner"]},
    "planner_password_reset": {"recipients": ["planner"]},
    "user_invite": {"staff_emails": ["ops@apc.test"]},
    "wedding_details_received": {"recipients": ["billing_contact"]},
}


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize(
    "over",
    [
        {"send_email": None, "send_sms": "on", "sms_body": "Hi"},
        {"recipients": ["booking_contact"]},
        {"trigger": "quote_sent"},
    ],
    ids=["channels", "recipients", "trigger"],
)
def test_who_how_and_when_of_an_invite_row_cannot_be_changed(client, key, over):
    _owner(client)
    if "recipients" in over:
        over = _OTHER_PEOPLE[key]
    before = listing.row_data(_row(key))

    resp = _edit(client, key, **over)

    assert resp.status_code == 200
    assert escape(editor.SET_BY_BUTTON) in resp.content.decode()
    assert listing.row_data(_row(key)) == before


@pytest.mark.parametrize("key", KEYS)
def test_the_wording_of_an_invite_row_stays_editable(client, key):
    _owner(client)
    body = _row(key).email_body.replace("Hi ", "Hello ", 1)
    assert _edit(client, key, email_body=body).status_code == 302
    assert _row(key).email_body == body


@pytest.mark.parametrize("key", KEYS)
def test_the_editor_and_preview_say_who_it_goes_to(client, key):
    _owner(client)
    html = client.get(reverse("message_edit", args=[key])).content.decode()
    assert escape(editor.WHO[key]) in html
    assert 'type="checkbox" name="recipients"' not in html
    assert editor.preview(_row(key), None)["to"] == [editor.WHO[key]]


def test_every_locked_row_says_who_it_goes_to():
    assert set(editor.WHO) == set(defaults.CLICKS) | set(defaults.INVITES)
    assert all(editor.is_locked(_row(key)) for key in KEYS)


def test_the_wedding_email_has_no_timing_and_no_other_row_can_use_its_trigger(client):
    assert editor.has_timing("wedding_details_received") is False
    assert editor.can_pick(_row("wedding_details_received"), "wedding_details_received")
    assert not editor.can_pick(_row("tp1_welcome"), "wedding_details_received")
    _owner(client)
    html = client.get(reverse("message_edit", args=["wedding_details_received"])).content.decode()
    assert "Sent the moment it happens (wedding details received)" in html
    assert (
        "Events: Wedding details received"
        not in client.get(reverse("messages_new")).content.decode()
    )


# --- sending -----------------------------------------------------------------------------


def test_a_planner_invite_sends_its_row_and_leaves_a_trail():
    with patch(DELIVER, return_value="pm-1") as deliver:
        account = _invite_planner()
    kwargs = deliver.call_args.kwargs
    assert kwargs["to"] == "dana@example.com"
    assert kwargs["template"] == "message"
    assert kwargs["internal"] is False
    assert "/planner/register/" in kwargs["context"]["body"]
    tp = TouchPoint.objects.get(kind="planner_invite")
    assert tp.lead_id is None and tp.status == TouchPoint.Status.SENT
    (delivery,) = Delivery.objects.filter(touchpoint=tp)
    assert (delivery.channel, delivery.address, delivery.status) == ("email", account.email, "sent")


def test_the_rows_wording_is_what_goes_out():
    MessageTemplate.objects.filter(key="planner_invite").update(
        subject="Welcome aboard, {account_name}"
    )
    _invite_planner()
    (message,) = mail.outbox
    assert message.subject == "Welcome aboard, Dana Planner"


def test_the_reset_carries_a_reset_link_and_the_invite_an_invite_link():
    account = _registered()
    mail.outbox.clear()
    assert planner_services.send_password_reset("dana@example.com") is True
    token = _link(mail.outbox[0].body, "/planner/password/").rstrip("/").rsplit("/", 1)[-1]
    assert planner_reset_token.check_token(account, token)
    assert not planner_token.check_token(account, token)
    assert TouchPoint.objects.filter(kind="planner_password_reset", lead=None).exists()


def test_a_reset_while_outbound_is_off_sends_nothing(settings):
    _registered()
    mail.outbox.clear()
    settings.OUTBOUND_ENABLED = False
    assert planner_services.send_password_reset("dana@example.com") is False
    assert mail.outbox == []
    tp = TouchPoint.objects.get(kind="planner_password_reset")
    assert tp.status == TouchPoint.Status.SKIPPED


def test_a_staff_invite_is_staff_mail_and_goes_while_outbound_is_off(settings):
    settings.OUTBOUND_ENABLED = False
    with patch(DELIVER, return_value="pm-1") as deliver:
        _, sent = _invite_user()
    assert sent is True
    assert deliver.call_args.kwargs["internal"] is True
    assert deliver.call_args.kwargs["to"] == "new.hire@allprocharter.com"


def test_a_failed_send_reports_false_and_keeps_the_user():
    with patch(DELIVER, return_value=None):
        user, sent = _invite_user()
    assert sent is False
    assert User.objects.filter(pk=user.pk).exists()
    assert TouchPoint.objects.get(kind="user_invite").status == TouchPoint.Status.FAILED


def test_the_wedding_email_leaves_a_trail_on_the_order():
    lead = _wedding_lead()
    assert public_services.send_wedding_confirmation(lead, base_url=BASE_URL) is True
    tp = TouchPoint.objects.get(kind="wedding_details_received")
    assert (tp.lead_id, tp.status) == (lead.pk, TouchPoint.Status.SENT)
    assert [m.to for m in mail.outbox] == [["jane@example.com"]]


@pytest.mark.parametrize("off", ["touchpoints", "master"])
def test_the_wedding_email_goes_at_once_whatever_the_automated_switches_say(settings, off):
    if off == "touchpoints":
        settings.TOUCHPOINTS_ENABLED = False
    else:
        NotificationConfig.objects.update_or_create(pk=1, defaults={"enabled": False})
    assert public_services.send_wedding_confirmation(_wedding_lead(), base_url=BASE_URL) is True
    assert len(mail.outbox) == 1


def test_a_lost_wedding_lead_still_gets_its_details_back():
    lead = _wedding_lead(status=Lead.Status.LOST)
    assert public_services.send_wedding_confirmation(lead, base_url=BASE_URL) is True


def test_an_interrupted_wedding_email_is_settled_not_sent_by_the_cron(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = _wedding_lead()
    row = _row("wedding_details_received")
    tp = TouchPoint.objects.create(
        lead=lead,
        message=row,
        kind=row.key,
        status=TouchPoint.Status.SCHEDULED,
        scheduled_for=timezone.now() - timedelta(minutes=10),
    )
    TouchPoint.objects.filter(pk=tp.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
    sender.run_touchpoints()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, sender.INTERRUPTED)
    assert mail.outbox == []


def test_an_unknown_email_renders_what_a_real_reset_renders():
    _registered()
    real, decoy = [], []
    for seen, email in ((real, "dana@example.com"), (decoy, "nobody@example.com")):
        with patch.object(loader, "get_template", side_effect=_recording(seen)):
            planner_services.send_password_reset(email)
    assert sorted(real) == sorted(decoy)
    assert "email/message.html" in real


_GET_TEMPLATE = loader.get_template


def _recording(seen: list):
    def get(name, *args, **kwargs):
        seen.append(name)
        return _GET_TEMPLATE(name, *args, **kwargs)

    return get


# --- the HTML parts keep today's generated parts --------------------------------------------


def _html(message) -> str:
    return message.alternatives[0][0]


def test_the_staff_invite_html_keeps_the_access_level_panel():
    _invite_user(role=User.Role.OWNER_ADMIN)
    (message,) = mail.outbox
    html = _html(message)
    assert "YOUR ACCESS LEVEL" in html
    assert "Sign in with this email address once your password is set." in html
    assert 'font-size:26px;font-weight:600;color:#17191D;padding:6px 0 4px;">Admin</div>' in html
    # The panel sits between the welcome and the button, as today.
    assert (
        html.index("as Admin.")
        < html.index("YOUR ACCESS LEVEL")
        < html.index("Set your password</a>")
    )
    # The text part never had it.
    assert "YOUR ACCESS LEVEL" not in message.body


@pytest.mark.parametrize(
    ("send", "parts"),
    [
        ("planner_invite", ["Create your account</a>"]),
        ("planner_password_reset", ["Choose a new password</a>"]),
        ("wedding", ["REFERENCE</div>", "Update my details</a>", "border-radius:999px"]),
    ],
)
def test_the_other_html_parts_keep_their_generated_parts(send, parts):
    if send == "planner_invite":
        _invite_planner()
    elif send == "planner_password_reset":
        _registered()
        mail.outbox.clear()
        planner_services.send_password_reset("dana@example.com")
    else:
        public_services.send_wedding_confirmation(_wedding_lead(), base_url=BASE_URL)
    html = _html(mail.outbox[-1])
    for part in parts:
        assert part in html, part


# --- fix round 1: offer only what the send fills; the reset decoy never fails ---------------

_AUDIENCE = {
    "planner_invite": "planner",
    "planner_password_reset": "planner",
    "user_invite": "staff",
    "wedding_details_received": "customer",
}


def _offered_for(key: str) -> list[str]:
    return [f.key for f in fields.fields_for(key, _AUDIENCE[key], key)]


def _use_every_offered_field(key: str) -> None:
    every = " ".join("{" + name + "}" for name in _offered_for(key))
    row = _row(key)
    row.email_body = f"{row.email_body}\n\n{every}"
    assert fields.validate(listing.row_data(row)) == {}, key
    row.save()


def test_the_invite_triggers_offer_exactly_what_their_callers_fill():
    assert set(_offered_for("planner_invite")) == {
        "account_name",
        "account_button",
        "company_name",
        "company_phone",
        "company_email",
    }
    assert set(_offered_for("planner_password_reset")) == set(_offered_for("planner_invite"))
    assert set(_offered_for("user_invite")) == {
        "account_first_name",
        "account_button",
        "role_label",
        "access_level",
        "company_name",
        "company_phone",
        "company_email",
    }


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("planner_invite", "quote_link"),
        ("planner_invite", "account_first_name"),
        ("planner_password_reset", "pay_link"),
        ("planner_password_reset", "first_name"),
        ("user_invite", "account_name"),
        ("user_invite", "quote_link"),
    ],
)
def test_a_field_the_send_cannot_fill_is_refused(key, field):
    data = listing.row_data(_row(key)) | {"subject": "Hi {" + field + "}"}
    assert field in fields.validate(data)["subject"]


def _send(key: str):
    if key == "planner_invite":
        _invite_planner()
    elif key == "planner_password_reset":
        _registered()
        mail.outbox.clear()
        assert planner_services.send_password_reset("dana@example.com") is True
    elif key == "user_invite":
        _invite_user()
    else:
        assert public_services.send_wedding_confirmation(_wedding_lead(), base_url=BASE_URL)


@pytest.mark.parametrize("key", KEYS)
def test_every_offered_field_renders_through_a_real_send(key):
    _use_every_offered_field(key)
    _send(key)
    tp = TouchPoint.objects.filter(kind=key).latest("pk")
    assert tp.status == TouchPoint.Status.SENT, tp.error
    assert not re.search(r"\{\w+\}", mail.outbox[-1].body)


def test_every_offered_field_renders_through_the_reset_decoy(caplog):
    _use_every_offered_field("planner_password_reset")
    seen: list = []
    with patch.object(loader, "get_template", side_effect=_recording(seen)):
        assert planner_services.send_password_reset("nobody@example.com") is False
    assert "email/message.html" in seen
    assert "decoy" not in caplog.text


def test_a_decoy_that_cannot_render_never_breaks_the_page(client, caplog):
    with patch("apps.messaging.rendering.render_email", side_effect=KeyError("unknown_field")):
        assert planner_services.send_password_reset("nobody@example.com") is False
    assert "decoy" in caplog.text


def test_the_reset_page_answers_the_same_for_a_known_and_an_unknown_email(client):
    _registered()
    _use_every_offered_field("planner_password_reset")
    url = reverse("planners:password_reset")
    known = client.post(url, {"email": "dana@example.com"})
    unknown = client.post(url, {"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content
    assert TouchPoint.objects.get(kind="planner_password_reset").status == "sent"


def test_the_staff_invite_text_keeps_one_blank_line_before_the_button():
    _invite_user()
    (message,) = mail.outbox
    assert (
        "You've been added to the All Pro Charter Lead Manager as Agent.\n\n"
        "Set your password to activate your account:\n"
    ) in message.body


def test_each_invite_preview_links_to_its_own_page():
    pages = {
        "planner_invite": "/planner/register/sample/",
        "planner_password_reset": "/planner/password/sample/",
        "user_invite": "/invite/sample/",
    }
    for key, page in pages.items():
        assert page in editor.preview(_row(key), None)["email"], key
