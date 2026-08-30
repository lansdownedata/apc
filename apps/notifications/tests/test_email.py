"""send_html_email — the project's only email primitive. Best-effort, never raises."""

import datetime
from unittest.mock import patch

import pytest
from django.core import mail

from apps.notifications.email import send_html_email

pytestmark = pytest.mark.django_db

BASE_CONTEXT = {
    "contact_name": "Shane Thomas",
    "quote_no": "APC-100041",
    "quote_total": "2118.90",
    "deposit_amount": "1059.45",
    "deposit_pct": 50,
    "quote_url": "https://example.com/quote/abc123/",
    "company_name": "All Pro Charter",
    "company_phone": "301-555-0100",
    "company_email": "info@allprocharter.com",
    "trip_count": 2,
    "expires_at": None,
    "logo_cid": "logo",
}


def _send(*, context=None, **overrides):
    kwargs = {
        "to": "customer@example.com",
        "subject": "Your All Pro Charter quote APC-100041",
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
    assert "APC-100041" in message.body, "the plain-text part must carry the quote number"
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
    assert mail.outbox[0].subject == "Your All Pro Charter quote APC-100041"
    assert mail.outbox[0].to == ["customer@example.com"]


def test_returns_false_and_does_not_raise_on_smtp_failure():
    with patch(
        "apps.notifications.email.EmailMultiAlternatives.send", side_effect=OSError("smtp down")
    ):
        assert _send() is False


def test_returns_false_on_a_blank_recipient():
    assert _send(to="") is False
    assert mail.outbox == []


def test_html_header_shows_the_logo_and_the_wordmark():
    """The banner references the crest logo as an inline CID image on the left, with
    the readable "All Pro Charter" wordmark beside it."""
    _send()
    html, _ = mail.outbox[0].alternatives[0]
    assert 'src="cid:logo"' in html, "logo not referenced as an inline CID image"
    assert "ALL PRO CHARTER" in html.upper(), "readable wordmark must still be present"


def test_html_header_falls_back_to_the_wordmark_without_a_logo():
    """When logo_cid is blank, the header degrades to the text wordmark alone —
    never a broken <img>."""
    _send(context={"logo_cid": ""})
    html, _ = mail.outbox[0].alternatives[0]
    assert "<img" not in html.lower(), "must not emit a broken image when logo_cid is blank"
    assert "ALL PRO CHARTER" in html.upper()


def test_supports_dark_mode():
    """Dark-mode-capable clients (Apple Mail/iOS) get prefers-color-scheme overrides;
    the dm-* classes carry the dark palette so inline light styles can be flipped."""
    _send()
    html, _ = mail.outbox[0].alternatives[0]
    assert 'content="light dark"' in html, "email must declare it supports dark mode"
    assert "prefers-color-scheme: dark" in html, "no dark-mode media query"
    assert "dm-ink" in html and "dm-card" in html, "dark-mode override classes missing"


def test_plain_text_part_is_complete_for_html_stripping_clients():
    """Strict/government clients that render only text/plain must still get every key
    fact — the quote number, total, deposit, and the link — as readable plain text."""
    _send()
    text = mail.outbox[0].body
    assert BASE_CONTEXT["quote_no"] in text
    assert BASE_CONTEXT["quote_total"] in text
    assert BASE_CONTEXT["deposit_amount"] in text
    assert BASE_CONTEXT["quote_url"] in text
    assert "<" not in text, "the plain-text part must be plain text, not HTML"


def test_no_template_comment_leaks_into_the_email():
    """Django {# #} comments are single-line only (the lexer regex is not DOTALL); a
    multi-line one renders its body as text. That shipped in the email header once and
    appeared at the top of the message — guard the rendered HTML."""
    _send()
    html, _ = mail.outbox[0].alternatives[0]
    assert "{#" not in html and "#}" not in html, "a template comment leaked into the email"
    assert "Brand display font" not in html, "the font comment body rendered as text"


def test_inline_image_is_embedded_with_its_content_id(tmp_path):
    """An inline_images entry is attached to the message with a matching Content-ID, so
    cid:<key> in the HTML resolves without a remote fetch."""
    png = tmp_path / "logo.png"
    png.write_bytes(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    _send(inline_images={"logo": str(png)})
    message = mail.outbox[0]
    cids = [a["Content-ID"] for a in message.attachments if hasattr(a, "get")]
    assert "<logo>" in cids, "inline image not attached with Content-ID <logo>"


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
