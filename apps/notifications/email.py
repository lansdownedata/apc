"""The project's only email primitive.

Podium (apps/integrations/podium.py) is plain-text with no subject line, so anything
designed goes out through django.core.mail instead. Delivery is best-effort — a failed
send is logged and reported, never raised, matching how send_quote already treats Podium.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def send_html_email(*, to: str, subject: str, template: str, context: dict) -> bool:
    """Render `templates/email/{template}.txt` + `.html` and send both parts.

    A multipart message is deliberate: HTML-only mail is penalised by spam filters.
    Returns True when the message was handed to the backend.
    """
    recipient = (to or "").strip()
    if not recipient:
        logger.warning("email %r not sent: no recipient", template)
        return False

    try:
        text_body = render_to_string(f"email/{template}.txt", context)
        html_body = render_to_string(f"email/{template}.html", context)
        message = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(html_body, "text/html")
        message.send()
    except Exception:
        logger.exception("email %r to %s failed", template, recipient)
        return False
    return True
