"""The project's only email primitive.

Podium (apps/integrations/podium.py) is plain-text with no subject line, so anything
designed goes out through django.core.mail instead. Delivery is best-effort — a failed
send is logged and reported, never raised, matching how send_quote already treats Podium.
"""

from __future__ import annotations

import logging
from email.mime.image import MIMEImage
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from apps.core import outbound

logger = logging.getLogger(__name__)


def send_html_email(
    *,
    to: str,
    subject: str,
    template: str,
    context: dict,
    inline_images: dict[str, str] | None = None,
    internal: bool = False,
) -> bool:
    """`deliver_html_email`, answering only whether it went: True when handed to the
    backend. Never raises."""
    return (
        deliver_html_email(
            to=to,
            subject=subject,
            template=template,
            context=context,
            inline_images=inline_images,
            internal=internal,
        )
        is not None
    )


def deliver_html_email(
    *,
    to: str,
    subject: str,
    template: str,
    context: dict,
    inline_images: dict[str, str] | None = None,
    internal: bool = False,
) -> str | None:
    """Render `templates/email/{template}.txt` + `.html` and send both parts.

    A multipart message is deliberate: HTML-only mail is penalised by spam filters.
    `inline_images` maps a Content-ID to a filesystem path; each is embedded in the
    message and referenced from the HTML as `cid:<key>`, so it renders without a remote
    fetch (email clients block remote images by default). Returns the provider's message id
    ("" when the backend gives none) once the message is handed to the backend, None when
    it was not sent; never raises — delivery is best-effort.

    Nothing is sent while the go-live switch is off (APC-90) unless `internal` says the
    recipient is one of our own staff.
    """
    recipient = (to or "").strip()
    if not recipient:
        logger.warning("email %r not sent: no recipient", template)
        return None
    if not outbound.allowed(kind=f"email {template!r}", recipient=recipient, internal=internal):
        return None

    try:
        text_body = render_to_string(f"email/{template}.txt", context)
        html_body = render_to_string(f"email/{template}.html", context)
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
            reply_to=[settings.COMPANY_EMAIL] if settings.COMPANY_EMAIL else None,
        )
        message.attach_alternative(html_body, "text/html")
        for cid, path in (inline_images or {}).items():
            img = MIMEImage(Path(path).read_bytes())
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=Path(path).name)
            message.attach(img)
        message.send()
    except Exception:
        logger.exception("email %r to %s failed", template, recipient)
        return None
    status = getattr(message, "anymail_status", None)
    return str(getattr(status, "message_id", "") or "")
