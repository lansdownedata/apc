import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts import two_factor
from apps.accounts.factories import UserFactory
from apps.accounts.models import SignInCode
from apps.accounts.two_factor import Delivery, ResendLimited, Subject
from apps.integrations import podium

pytestmark = pytest.mark.django_db

SEND = "apps.integrations.podium.send_message"
SMS_BODY = (
    r"All Pro Charter: your sign-in code is (\d{6})\. "
    r"It expires in 10 minutes\. Never share this code\."
)


@pytest.fixture
def subject() -> Subject:
    user = UserFactory(phone="+15715550142", first_name="Tarrick", email="t@example.com")
    return two_factor.staff_subject(user)


def test_sms_goes_through_podium_as_internal(subject):
    with patch(SEND, return_value={"data": {"uid": "m-1"}}) as send:
        d = two_factor.send_code(subject)
    assert d == Delivery(channel="sms", fell_back=False)
    assert d.sent
    kw = send.call_args.kwargs
    assert kw["identifier"] == subject.phone and kw["internal"] is True
    assert re.fullmatch(SMS_BODY, kw["body"])
    assert SignInCode.objects.get().podium_message_uid == "m-1"


def test_top_level_uid_is_read_too(subject):
    with patch(SEND, return_value={"uid": "m-2"}):
        two_factor.send_code(subject)
    assert SignInCode.objects.get().podium_message_uid == "m-2"


def test_podium_error_falls_back_to_email_with_the_same_code(subject, mailoutbox):
    with patch(SEND, side_effect=podium.PodiumAPIError(500, "boom")) as send:
        d = two_factor.send_code(subject)
    assert d == Delivery(channel="email", fell_back=True)
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.subject == "Your All Pro Charter sign-in code"
    assert mail.to == ["t@example.com"]
    sms_code = re.fullmatch(SMS_BODY, send.call_args.kwargs["body"]).group(1)
    assert f"{sms_code[:3]} {sms_code[3:]}" in mail.body
    assert SignInCode.objects.get().channel == "email"


def test_any_exception_from_podium_falls_back(subject, mailoutbox):
    with patch(SEND, side_effect=TimeoutError()):
        d = two_factor.send_code(subject)
    assert d == Delivery(channel="email", fell_back=True)
    assert len(mailoutbox) == 1


def test_no_phone_goes_straight_to_email(mailoutbox):
    s = two_factor.staff_subject(UserFactory(phone="", email="n@example.com"))
    with patch(SEND) as send:
        d = two_factor.send_code(s)
    send.assert_not_called()
    assert d == Delivery(channel="email", fell_back=False)
    assert len(mailoutbox) == 1


def test_both_failing_reports_nothing_sent(subject):
    with (
        patch(SEND, side_effect=podium.PodiumAPIError(500, "boom")),
        patch("apps.accounts.two_factor.send_html_email", return_value=False),
    ):
        d = two_factor.send_code(subject)
    assert d == Delivery(channel="", fell_back=True)
    assert not d.sent


def test_email_request_voids_sms_code(subject, mailoutbox):
    base = timezone.now()
    with patch(SEND, return_value={"uid": "m-3"}) as send:
        two_factor.send_code(subject)
    old = re.fullmatch(SMS_BODY, send.call_args.kwargs["body"]).group(1)
    with patch("django.utils.timezone.now", return_value=base + timedelta(seconds=61)):
        d = two_factor.send_code(subject, prefer="email")
        assert d == Delivery(channel="email", fell_back=False)
        assert two_factor.verify(subject, old) == two_factor.Verdict.WRONG
    assert len(mailoutbox) == 1


def test_email_request_counts_toward_the_hourly_limit(subject, mailoutbox):
    base = timezone.now()
    for i in range(two_factor.RESEND_PER_HOUR):
        with patch("django.utils.timezone.now", return_value=base + timedelta(seconds=61 * i)):
            two_factor.send_code(subject, prefer="email")
    with (
        patch(
            "django.utils.timezone.now",
            return_value=base + timedelta(seconds=61 * two_factor.RESEND_PER_HOUR),
        ),
        pytest.raises(ResendLimited),
    ):
        two_factor.send_code(subject, prefer="email")


def test_send_goes_while_outbound_is_off(subject, settings, mailoutbox):
    settings.OUTBOUND_ENABLED = False
    with patch("apps.integrations.podium._request", return_value={"uid": "m-4"}) as req:
        d = two_factor.send_code(subject)
    assert d.channel == "sms"
    req.assert_called_once()
    s2 = two_factor.staff_subject(UserFactory(phone="", email="x@example.com"))
    two_factor.send_code(s2)
    assert len(mailoutbox) == 1


def test_email_html_shows_spaced_code_and_reason(subject, mailoutbox):
    with patch(SEND, side_effect=podium.PodiumAPIError(500, "boom")):
        two_factor.send_code(subject)
    html = mailoutbox[0].alternatives[0][0]
    assert re.search(r"\d{3}&nbsp;\d{3}", html)
    assert "ending in 0142" in html
    assert "Hi Tarrick," in html
    assert "someone has your password" in html


def test_mask_phone_and_email():
    assert two_factor.mask_phone("+15715550142") == "0142"
    assert two_factor.mask_email("moe@example.com") == "m•••@example.com"


def test_is_code_echo_by_uid_and_by_body(subject):
    with patch(SEND, return_value={"uid": "m-5"}):
        two_factor.send_code(subject)
    assert two_factor.is_code_echo("m-5", "anything")
    assert two_factor.is_code_echo("other", "X: your sign-in code is 123456. It expires")
    assert not two_factor.is_code_echo("", "On our way!")
    assert not two_factor.is_code_echo("", "")


def test_sms_send_uses_a_short_timeout(subject):
    with patch(SEND, return_value={"uid": "m-3"}) as send:
        two_factor.send_code(subject)
    assert send.call_args.kwargs["timeout"] == two_factor.SMS_TIMEOUT == 8


def test_a_podium_http_timeout_falls_back_to_one_email(subject, mailoutbox):
    import requests

    with (
        patch("apps.integrations.podium.get_credential"),
        patch("apps.integrations.podium.requests.request", side_effect=requests.Timeout()) as req,
    ):
        d = two_factor.send_code(subject)
    assert req.call_args.kwargs["timeout"] == 8
    assert d == Delivery(channel="email", fell_back=True)
    assert len(mailoutbox) == 1


def test_the_failure_log_never_carries_the_code(subject, caplog):
    def refuse(**kwargs):
        code = re.search(r"\d{6}", kwargs["body"]).group(0)
        raise podium.PodiumAPIError(
            f'400 POST https://api.podium.com/v4/messages → {{"echo": "{code}", '
            f'"message": "Contact opted out"}} body {code}'
        )

    with patch(SEND, side_effect=refuse), caplog.at_level("DEBUG"):
        two_factor.send_code(subject)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Podium refused the message: Contact opted out" in text
    assert "PodiumAPIError" in text
    assert not re.search(r"\b\d{6}\b", text)
