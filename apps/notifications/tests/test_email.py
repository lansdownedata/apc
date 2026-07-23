"""send_html_email — the project's only email primitive. Best-effort, never raises."""

import datetime
from unittest.mock import patch

import pytest
from django.core import mail

from apps.notifications.email import send_html_email

pytestmark = pytest.mark.django_db

BASE_CONTEXT = {
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
}


def _send(*, context=None, **overrides):
    kwargs = {
        "to": "customer@example.com",
        "subject": "Your All Pro Charter quote Q-1041",
        "template": "quote_sent",
        "context": {**BASE_CONTEXT, **(context or {})},
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


def test_plain_text_part_is_not_html_escaped():
    """Django autoescapes .txt templates too, so a name/company with & rendered as
    "&amp;" in the plain-text body — wrong for plain text. The HTML part still escapes."""
    _send(context={"contact_name": "Priya & Daniel", "company_name": "Smith & Co Charter"})
    message = mail.outbox[0]
    assert "Priya & Daniel" in message.body, "plain-text body must not HTML-escape the name"
    assert "&amp;" not in message.body
    # the HTML part must still escape — & belongs as &amp; there
    html = message.alternatives[0][0]
    assert "Priya &amp; Daniel" in html


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


def test_reply_to_uses_configured_company_email(settings):
    """Replies must land on a monitored inbox, not the noreply@ From address."""
    settings.COMPANY_EMAIL = "info@allprocharter.com"
    _send()
    assert mail.outbox[0].reply_to == ["info@allprocharter.com"]


def test_reply_to_is_empty_when_company_email_is_blank(settings):
    """A blank COMPANY_EMAIL must not blow up send_html_email's never-raises contract."""
    settings.COMPANY_EMAIL = ""
    assert _send() is True
    assert not mail.outbox[0].reply_to


def test_blank_quote_url_renders_no_dead_button():
    """A blank quote_url must suppress the CTA entirely rather than render <a href="">."""
    assert _send(context={"quote_url": ""}) is True
    message = mail.outbox[0]
    html, _ = message.alternatives[0]
    assert '<a href="">' not in html
    assert "View your full itinerary and book:" not in message.body


def test_expires_at_renders_the_formatted_date():
    _send(context={"expires_at": datetime.date(2026, 8, 15)})
    message = mail.outbox[0]
    html, _ = message.alternatives[0]
    assert "August 15, 2026" in html
    assert "August 15, 2026" in message.body


def test_unknown_template_returns_false_and_does_not_raise():
    """A typo'd template name (Task 7's likeliest failure mode) must never raise."""
    assert _send(template="does_not_exist") is False
    assert mail.outbox == []
