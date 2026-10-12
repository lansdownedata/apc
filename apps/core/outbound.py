"""The go-live switch (APC-90).

`OUTBOUND_ENABLED` decides whether the portal may contact anyone outside the company.
Off — the default — every customer and affiliate send is refused: Podium texts and emails,
review invitations, and our own HTML mail. That lets every cron job be registered and run
before go-live without a customer hearing from a system nobody has announced.

The check lives in the three send primitives (`podium.send_message`,
`podium.create_review_invitation`, `notifications.email.send_html_email`), not at their
callers, so it fails closed: a new send is refused until someone decides otherwise. Mail to
our own staff — digests, reports, invites — opts out with `internal=True`; it is how the
office watches the portal while the switch is off.

`TOUCHPOINTS_ENABLED` stays a separate, narrower switch: it says whether the touch-point
engine runs at all. This one says whether what it produces may leave.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

OFF_MESSAGE = "Outbound messaging is off, so nothing was sent."


class OutboundDisabled(Exception):
    """An external send was attempted while the go-live switch is off."""

    def __init__(self, message: str = OFF_MESSAGE) -> None:
        super().__init__(message)


def enabled() -> bool:
    """Whether the portal may contact customers and affiliates. A missing setting is off."""
    return bool(getattr(settings, "OUTBOUND_ENABLED", False))


def allowed(*, kind: str, recipient: str, internal: bool = False) -> bool:
    """Whether this one send may go. A refusal is logged with what it was and who for, so
    a dry run before go-live leaves a record of everything that would have gone out."""
    if internal or enabled():
        return True
    logger.info("outbound off: %s to %s not sent", kind, recipient)
    return False
