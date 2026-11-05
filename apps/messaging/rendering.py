"""The email a message row sends (spec C sections 3 and 8).

A row's email is its wording rendered as plain text, wrapped in the branded layout
(`templates/email/message.{txt,html}`, which extend `base_email.html`). Reports and long
emails keep their generated parts in code as single placeholders ("Report table", "Receipt
lines", "Trip & price summary"): a `Block` carries a part's plain text and its HTML, rendered
from the partials in `templates/email/blocks/`.

In the HTML part every value and the wording itself are escaped; paragraphs split on blank
lines into `<p>`s with the invite's inline styles; a block placeholder alone on its line
renders the block's own HTML there. The text part is the rendered wording and nothing else.
The `--` signature at the end of the wording stays out of the HTML part, whose footer
already carries the company's name and contacts.
"""

import re
from dataclasses import dataclass

from django.conf import settings
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from .touchpoint_templates import render

# The inline styles of templates/email/planner_invite.html: its greeting, then its body copy.
_FIRST = '<p class="dm-ink" style="margin:0 0 6px;font-size:17px;color:#17191D;">'
_BODY = '<p class="dm-muted" style="margin:0 0 28px;font-size:16px;color:#4b5563;line-height:1.6;">'
_BLOCK = '<div style="margin:0 0 28px;">'
# Stands in for a block's HTML while the rest of the wording is escaped; escaping never
# touches it, and no placeholder value can produce it.
_MARK = "\x00block:{}\x00"
_MARKED = re.compile(r"\x00block:(\w+)\x00")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SIGNATURE = "--"
LOGO = "brand/apc-logo-email.png"


@dataclass(frozen=True)
class Block:
    """A generated part of an email: its plain text and its (trusted, template-rendered)
    HTML."""

    text: str
    html: str


def block(name: str, context: dict) -> Block:
    """Render `templates/email/blocks/<name>.txt` and `.html` into a Block."""
    return Block(
        text=render_to_string(f"email/blocks/{name}.txt", context),
        html=render_to_string(f"email/blocks/{name}.html", context),
    )


def button(label: str, url: str) -> Block:
    """The gold call-to-action button; in plain text, the label and the link."""
    return block("button", {"label": label, "url": url})


def logo_path() -> str | None:
    """The masthead logo PNG (email clients can't render the SVG), attached inline as
    `cid:logo`; None when it isn't collectable."""
    return finders.find(LOGO)


def texts(ctx: dict[str, str], blocks: dict[str, Block]) -> dict[str, str]:
    """The context with each block as its plain text: for a text, Podium email, a subject
    and the plain-text part."""
    return {**ctx, **{name: b.text for name, b in blocks.items()}}


def _body_html(email_body: str, ctx: dict[str, str], blocks: dict[str, Block]) -> SafeString:
    lines = []
    for line in email_body.split("\n"):
        if line.strip() == _SIGNATURE:
            break
        alone = _PLACEHOLDER.fullmatch(line.strip())
        if alone and alone.group(1) in blocks:
            if blocks[alone.group(1)].html.strip():
                lines.append(_MARK.format(alone.group(1)))
            continue
        lines.append(line)
    rendered = escape(render("\n".join(lines), texts(ctx, blocks)))

    parts: list[str] = []
    paragraph: list[str] = []

    def close() -> None:
        if paragraph:
            opener = _FIRST if not parts else _BODY
            parts.append(opener + "<br>\n".join(paragraph) + "</p>")
            paragraph.clear()

    for line in rendered.split("\n"):
        marked = _MARKED.fullmatch(line.strip())
        if marked:
            close()
            parts.append(_BLOCK + blocks[marked.group(1)].html + "</div>")
        elif line.strip():
            paragraph.append(line.strip())
        else:
            close()
    close()
    # Every value and every line of wording was escaped above; only the blocks' own
    # template-rendered HTML goes in as it is.
    return mark_safe("\n".join(parts))


def layout_context(
    row, ctx: dict[str, str], blocks: dict[str, Block], *, logo_cid: str = ""
) -> tuple[str, dict]:
    """The subject, and the context `email/message.{txt,html}` render the row's email from:
    `body` (the text part) and `body_html` (the HTML part's paragraphs and blocks)."""
    plain = texts(ctx, blocks)
    subject = render(row.subject, plain) if row.subject else ""
    email_body = row.email_body or ""
    return subject, {
        "subject": subject,
        "body": render(email_body, plain),
        "body_html": _body_html(email_body, ctx, blocks),
        "logo_cid": logo_cid,
        "company_name": ctx.get("company_name", settings.COMPANY_NAME),
        "company_phone": ctx.get("company_phone", settings.COMPANY_PHONE),
        "company_email": ctx.get("company_email", settings.COMPANY_EMAIL),
    }


def render_email(
    row, ctx: dict[str, str], blocks: dict[str, Block], *, logo_cid: str = ""
) -> tuple[str, str, str]:
    """(subject, text, html) of the row's email for this context."""
    subject, layout = layout_context(row, ctx, blocks, logo_cid=logo_cid)
    return (
        subject,
        render_to_string("email/message.txt", layout),
        render_to_string("email/message.html", layout),
    )
