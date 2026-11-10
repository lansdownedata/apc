"""Review fixes: password reset — timing, per-email cap, its own 7-day links (APC-66, APC-130)."""

import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.template import loader
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.planners import services
from apps.planners.tokens import planner_reset_token, planner_token

pytestmark = pytest.mark.django_db

SEND = "apps.notifications.email.deliver_html_email"
PASSWORD = "a-long-pass-phrase-77"


@pytest.fixture
def registered():
    contact = ContactFactory(name="Dana Planner", email="dana@example.com", phone="(571) 555-0142")
    with patch(SEND, return_value="pm-1"):
        account, _ = services.invite(contact, actor=UserFactory())
    return services.register(account, PASSWORD)


def _post(client, email):
    return client.post(reverse("planners:password_reset"), {"email": email})


def test_reset_mail_carries_a_reset_link_not_an_invite_link(registered):
    with patch(SEND, return_value="pm-1") as send:
        services.send_password_reset("dana@example.com")
    url = re.search(r"\S+/planner/password/\S+", send.call_args.kwargs["context"]["body"]).group(0)
    token = url.rstrip("/").split("/")[-1]
    assert planner_reset_token.check_token(registered, token)
    assert not planner_token.check_token(registered, token)


def test_reset_links_last_seven_days_as_the_email_says(registered):
    """APC-130: the reset email promises 7 days, so the link lives 7 days, like an invite."""
    with patch.object(planner_reset_token, "_now", return_value=datetime.now() - timedelta(days=6)):
        fresh = planner_reset_token.make_token(registered)
    with patch.object(planner_reset_token, "_now", return_value=datetime.now() - timedelta(days=8)):
        stale = planner_reset_token.make_token(registered)
    assert planner_reset_token.check_token(registered, fresh) is True
    assert planner_reset_token.check_token(registered, stale) is False


def test_the_sent_page_promises_the_links_real_life(client, registered):
    """PLN-09: the page said three days while the link (and the email) said seven."""
    with patch(SEND, return_value="pm-1"):
        page = _post(client, "dana@example.com").content.decode()
    assert "It works for seven days." in page
    assert "three days" not in page


def test_the_sent_page_follows_the_reset_links_timeout(client, registered):
    with (
        patch.object(planner_reset_token, "timeout", timedelta(days=5)),
        patch(SEND, return_value="pm-1"),
    ):
        page = _post(client, "dana@example.com").content.decode()
    assert "It works for five days." in page
    assert "seven days" not in page


def test_the_reset_page_refuses_an_expired_reset_link(client, registered):
    with patch.object(planner_reset_token, "_now", return_value=datetime.now() - timedelta(days=8)):
        token = planner_reset_token.make_token(registered)
    uid = urlsafe_base64_encode(force_bytes(registered.pk))
    url = reverse("planners:set_password", kwargs={"uidb64": uid, "token": token})
    assert client.get(url, follow=True).status_code == 400


def test_an_unknown_email_does_the_same_template_work(registered):
    real_get = loader.get_template
    with patch(SEND) as send, patch.object(loader, "get_template", wraps=real_get) as render:
        services.send_password_reset("nobody@example.com")
    send.assert_not_called()
    # the templates a real reset renders: its button, its preview line, the .txt and .html
    assert sorted(c.args[0] for c in render.call_args_list) == [
        "email/blocks/link_button.html",
        "email/blocks/link_button.txt",
        "email/message.html",
        "email/message.txt",
        "email/preheaders/planner_password_reset.html",
    ]


def test_a_per_email_cap_applies_silently(client, registered):
    with patch(SEND, return_value="pm-1") as send:
        for i in range(services.RESET_PER_EMAIL + 2):
            # a fresh IP each time, so only the per-email cap can bite
            client.post(
                reverse("planners:password_reset"),
                {"email": "Dana@example.com"},
                REMOTE_ADDR=f"10.0.0.{i + 1}",
            )
    assert send.call_count == services.RESET_PER_EMAIL


def test_an_invited_planner_gets_no_mail_and_the_page_says_what_to_do(client):
    contact = ContactFactory(email="ivy@example.com", phone="(571) 555-0142")
    with patch(SEND, return_value="pm-1"):
        services.invite(contact, actor=UserFactory())
    with patch(SEND) as send:
        html = _post(client, "ivy@example.com").content.decode()
    send.assert_not_called()
    assert "ask your coordinator to resend your invitation" in html
    assert html == _post(client, "nobody@example.com").content.decode()


def test_no_reset_mail_without_a_public_base_url(registered, settings):
    settings.PUBLIC_BASE_URL = ""
    with patch(SEND) as send:
        assert services.send_password_reset("dana@example.com") is False
    send.assert_not_called()
