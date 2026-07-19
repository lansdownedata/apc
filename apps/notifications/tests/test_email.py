"""send_html_email — the project's only email primitive. Best-effort, never raises."""

from unittest.mock import patch

import pytest
from django.core import mail

from apps.notifications.email import send_html_email

pytestmark = pytest.mark.django_db


def _send(**overrides):
    kwargs = {
        "to": "customer@example.com",
        "subject": "Your All Pro Charter quote Q-1041",
        "template": "quote_sent",
        "context": {
            "contact_name": "Shane Thomas",
            "quote_no": "Q-1041",
            "quote_total": "2118.90",
            "deposit_amount": "1059.45",
            "deposit_pct": 50,
            "quote_url": "https://example.com/quote/abc123/",
            "company_name": "All Pro Charter",
            "company_phone": "301-555-0100",
            "company_email": "info@allprocharter.com",
            "trip_count": 2,
            "expires_at": None,
        },
    }
    kwargs.update(overrides)
    return send_html_email(**kwargs)


def test_sends_and_returns_true():
    assert _send() is True
    assert len(mail.outbox) == 1


def test_sends_both_a_text_and_an_html_part():
    _send()
    message = mail.outbox[0]
    assert "Q-1041" in message.body, "the plain-text part must carry the quote number"
    html, mimetype = message.alternatives[0]
    assert mimetype == "text/html"
    assert "https://example.com/quote/abc123/" in html


def test_subject_and_recipient():
    _send()
    assert mail.outbox[0].subject == "Your All Pro Charter quote Q-1041"
    assert mail.outbox[0].to == ["customer@example.com"]


def test_returns_false_and_does_not_raise_on_smtp_failure():
    with patch(
        "apps.notifications.email.EmailMultiAlternatives.send", side_effect=OSError("smtp down")
    ):
        assert _send() is False


def test_returns_false_on_a_blank_recipient():
    assert _send(to="") is False
    assert mail.outbox == []


def test_html_part_renders_the_text_wordmark_not_an_image():
    """Decision 1 override: no logo.png exists, so the header must be a styled text
    wordmark using company_name — never an <img> tag pointed at a logo file."""
    _send()
    html, _ = mail.outbox[0].alternatives[0]
    assert "ALL PRO CHARTER" in html.upper()
    assert "<img" not in html.lower()
