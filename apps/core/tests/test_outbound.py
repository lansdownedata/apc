"""The go-live switch (APC-90): one setting that decides whether anything leaves the building.

Off, nothing reaches a customer or an affiliate — not a text, not an email, not a review
invitation. Mail to our own staff is not behind it: digests and reports are how the office
watches the portal run before it is live.

The gate sits in the three send primitives rather than at each caller, so a send somebody
adds next month is refused by default instead of slipping out.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.core import outbound
from apps.integrations import podium
from apps.integrations.factories import PodiumCredentialFactory
from apps.notifications.email import send_html_email

pytestmark = pytest.mark.django_db


@pytest.fixture
def off(settings):
    settings.OUTBOUND_ENABLED = False


@pytest.fixture
def transport():
    """Podium's HTTP transport. A call here is a message that really left."""
    PodiumCredentialFactory(access_token="AT", expires_at=timezone.now() + timedelta(hours=5))
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"uid": "msg-1"}
    with patch.object(podium.requests, "request", return_value=fake) as request:
        yield request


def _email(**over) -> bool:
    kwargs = {
        "to": "someone@example.com",
        "subject": "Trip cancelled",
        "template": "user_invite",
        "context": {"first_name": "Sam", "invite_url": "https://x.test/i/", "role_label": "Agent"},
    }
    kwargs.update(over)
    return send_html_email(**kwargs)


# --- the switch ----------------------------------------------------------------------


def test_the_switch_reads_the_setting(settings):
    settings.OUTBOUND_ENABLED = True
    assert outbound.enabled() is True
    settings.OUTBOUND_ENABLED = False
    assert outbound.enabled() is False


def test_a_missing_setting_means_off(settings):
    del settings.OUTBOUND_ENABLED
    assert outbound.enabled() is False


# --- Podium messages -----------------------------------------------------------------


def test_a_text_is_refused_while_off(off, transport):
    with pytest.raises(outbound.OutboundDisabled):
        podium.send_message(identifier="+15715551212", body="Your quote is ready")
    transport.assert_not_called()


def test_a_text_goes_out_once_on(transport):
    assert podium.send_message(identifier="+15715551212", body="hi") == {"uid": "msg-1"}
    transport.assert_called_once()


def test_a_text_to_our_own_staff_goes_out_while_off(off, transport):
    podium.send_message(identifier="+15715551212", body="2 CRITICAL exceptions", internal=True)
    transport.assert_called_once()


def test_a_review_invitation_is_refused_while_off(off, transport):
    with pytest.raises(outbound.OutboundDisabled):
        podium.create_review_invitation(phone="+15715551212", first_name="A", last_name="B")
    transport.assert_not_called()


# --- email ---------------------------------------------------------------------------


def test_an_email_is_not_sent_while_off(off):
    assert _email() is False
    assert mail.outbox == []


def test_an_email_goes_out_once_on():
    assert _email() is True
    assert len(mail.outbox) == 1


def test_an_email_to_our_own_staff_goes_out_while_off(off):
    assert _email(internal=True) is True
    assert len(mail.outbox) == 1


def test_a_refused_send_is_logged_with_who_it_was_for(off, caplog):
    with caplog.at_level("INFO", logger="apps.core.outbound"):
        _email(to="bride@example.com")
    assert "bride@example.com" in caplog.text
    assert "user_invite" in caplog.text
