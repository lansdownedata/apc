"""The email layout (spec C sections 3 and 8): a message row's wording, wrapped in the branded
layout, with the generated parts ("Report table", "Receipt lines", ...) as blocks.

Rendering only: nothing here sends.
"""

from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string

from apps.messaging import rendering
from apps.messaging.factories import MessageRowFactory
from apps.messaging.rendering import Block

pytestmark = pytest.mark.django_db

GREETING = 'class="dm-ink" style="margin:0 0 6px;font-size:17px;color:#17191D;"'
BODY = 'class="dm-muted" style="margin:0 0 28px;font-size:16px;color:#4b5563;line-height:1.6;"'
CTX = {
    "first_name": "Dana",
    "quote_no": "Q-100",
    "company_name": "All Pro Charter",
    "company_phone": "301-555-0100",
    "company_email": "info@apc.test",
}


def _row(email_body: str, subject: str = "Your quote {quote_no}"):
    return MessageRowFactory(subject=subject, email_body=email_body, send_sms=False)


def test_returns_subject_text_and_html():
    row = _row("Hi {first_name},\n\nYour quote {quote_no} is ready.")

    subject, text, html = rendering.render_email(row, CTX, {})

    assert subject == "Your quote Q-100"
    assert text == "Hi Dana,\n\nYour quote Q-100 is ready."
    assert html.lstrip().startswith("<!doctype html>")
    assert "<title>Your quote Q-100</title>" in html


def test_text_part_equals_the_paragraphs():
    """The plain-text part is the row's wording, rendered, and nothing else: the signature is
    part of the wording, so the text part carries exactly what the Admin wrote."""
    row = _row("Hi {first_name},\n\nLine one\nline two.\n\n--\n{company_name}")

    _, text, _ = rendering.render_email(row, CTX, {})

    assert text == "Hi Dana,\n\nLine one\nline two.\n\n--\nAll Pro Charter"


def test_paragraphs_split_on_blank_lines_with_the_invite_styles():
    row = _row("Hi {first_name},\n\nLine one\nline two.\n\nLast word.")

    _, _, html = rendering.render_email(row, CTX, {})

    assert f"<p {GREETING}>Hi Dana,</p>" in html
    assert f"<p {BODY}>Line one<br>\nline two.</p>" in html
    assert f"<p {BODY}>Last word.</p>" in html


def test_plain_placeholders_are_escaped_in_the_html_part():
    row = _row("Hi {first_name},\n\nThanks.", subject="Hello {first_name}")
    ctx = {**CTX, "first_name": "<script>alert(1)</script>"}

    subject, text, html = rendering.render_email(row, ctx, {})

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # Plain text is not HTML: it carries the name as typed.
    assert text.startswith("Hi <script>alert(1)</script>,")
    assert subject == "Hello <script>alert(1)</script>"


def test_the_wording_itself_is_escaped_in_the_html_part():
    row = _row("Fish & chips <b>now</b>")

    _, text, html = rendering.render_email(row, CTX, {})

    assert "Fish &amp; chips &lt;b&gt;now&lt;/b&gt;" in html
    assert text == "Fish & chips <b>now</b>"


def test_a_block_alone_on_its_line_renders_its_own_html():
    row = _row("Hi {first_name},\n\nHere it is:\n{report_table}\nThat's all.")
    table = Block(text="  Q-1 · Dana\n  Q-2 · Bea", html="<table><tr><td>Q-1</td></tr></table>")

    _, text, html = rendering.render_email(row, CTX, {"report_table": table})

    assert text == "Hi Dana,\n\nHere it is:\n  Q-1 · Dana\n  Q-2 · Bea\nThat's all."
    # The block breaks the paragraph: text before it, the block's own markup, text after.
    before = html.index(f"<p {BODY}>Here it is:</p>")
    block = html.index("<table><tr><td>Q-1</td></tr></table>")
    after = html.index(f"<p {BODY}>That&#x27;s all.</p>")
    assert before < block < after
    assert "Q-1 · Dana" not in html


def test_a_block_inside_a_sentence_renders_its_text_escaped():
    row = _row("Totals: {receipt_lines} today.")
    block = Block(text="$5 & up", html="<table>never inline</table>")

    _, text, html = rendering.render_email(row, CTX, {"receipt_lines": block})

    assert text == "Totals: $5 & up today."
    assert "Totals: $5 &amp; up today." in html
    assert "never inline" not in html


def test_an_empty_block_drops_its_line():
    row = _row("Before\n{report_table}\nAfter")

    _, text, html = rendering.render_email(row, CTX, {"report_table": Block(text="", html="")})

    assert text == "Before\nAfter"
    assert f"<p {GREETING}>Before<br>\nAfter</p>" in html


def test_the_signature_stays_out_of_the_html_part():
    """The layout's footer carries the company's name and contacts, so the text part's
    `--` signature is not repeated in the HTML part."""
    row = _row("Hi {first_name},\n\nThanks.\n\n--\n{company_name}\n{company_email}")

    _, text, html = rendering.render_email(row, CTX, {})

    assert text.endswith("--\nAll Pro Charter\ninfo@apc.test")
    assert "--" not in html.split("<body", 1)[1].split("dm-footer", 1)[0]
    assert html.count("info@apc.test") == 2  # the footer's mailto link and its text only


def test_the_layout_carries_the_company_and_the_logo_only_when_asked():
    """The logo is opt-in: only the emails that carry it today (the quote, the affiliate
    offer) ask for it."""
    row = _row("Hi")

    _, _, plain = rendering.render_email(row, CTX, {})
    _, _, branded = rendering.render_email(row, CTX, {}, logo_cid="logo")

    assert "All Pro Charter" in plain and "cid:logo" not in plain
    assert 'src="cid:logo"' in branded


def test_a_value_can_never_stand_in_for_a_block():
    """The block marker is built from a NUL byte; a value carrying one is stripped of it, so
    no customer-typed name can pull a block's HTML into the email."""
    row = _row("Hi {first_name}\n\n{report_table}")
    ctx = {**CTX, "first_name": "\x00block:report_table\x00"}
    table = Block(text="rows", html="<table>rows</table>")

    _, _, html = rendering.render_email(row, ctx, {"report_table": table})

    assert html.count("<table>rows</table>") == 1
    assert "\x00" not in html


def test_layout_context_feeds_the_message_templates():
    """The transport hands `layout_context` to `email/message.{txt,html}`; rendering those
    gives exactly what `render_email` returns."""
    row = _row("Hi {first_name},\n\n{report_table}")
    blocks = {"report_table": Block(text="rows", html="<table>rows</table>")}

    subject, ctx = rendering.layout_context(row, CTX, blocks, logo_cid="logo")

    assert (subject, render_to_string("email/message.txt", ctx)) == rendering.render_email(
        row, CTX, blocks, logo_cid="logo"
    )[:2]
    assert (
        render_to_string("email/message.html", ctx)
        == rendering.render_email(row, CTX, blocks, logo_cid="logo")[2]
    )


# --- the block partials ----------------------------------------------------------------


def test_button_block():
    block = rendering.button("Create your account", "https://apc.test/r?a=1&b=2")

    assert block.text == "Create your account:\nhttps://apc.test/r?a=1&b=2"
    assert 'href="https://apc.test/r?a=1&amp;b=2"' in block.html
    assert ">Create your account</a>" in block.html
    assert "background:#C7A24E" in block.html


def test_block_renders_a_partial_pair():
    block = rendering.block(
        "quote_summary",
        {
            "quote_no": "Q-1",
            "contact_name": "Priya & Dan",
            "quote_total": "1,000.00",
            "deposit_pct": 50,
            "deposit_amount": "500.00",
            "on_account": False,
        },
    )

    assert "PREPARED FOR    Priya & Dan" in block.text  # plain text is not escaped
    assert "A 50% deposit of $500.00 reserves your booking." in block.text
    assert "Priya &amp; Dan" in block.html
    assert "ESTIMATED TOTAL" in block.html


def test_report_table_partials_are_named_per_report():
    rows = [
        SimpleNamespace(
            url="https://apc.test/o/1",
            quote_no="Q-1",
            customer="Dana",
            email="",
            phone="",
            trips=1,
            earliest_pickup=None,
            days_out=3,
            overdue_days=0,
            total=10,
            collected=0,
        )
    ]

    block = rendering.block("report_table/deposit_report", {"rows": rows})

    assert "Q-1 · Dana" in block.text
    assert 'href="https://apc.test/o/1"' in block.html
