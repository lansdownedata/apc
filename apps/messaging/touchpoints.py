"""Touch-point scheduling + sending (spec 2026-07-11, client cadence TP1-TP8)."""

from django.utils import timezone

from .models import TouchPoint
from .touchpoint_templates import TEMPLATES

_QUOTE_KINDS = (
    TouchPoint.Kind.TP3_QUOTE_SENT_SMS,
    TouchPoint.Kind.TP4_VIEWED_SMS,
    TouchPoint.Kind.TP5_VIEWED_EMAIL,
    TouchPoint.Kind.TP6_QUOTE_FOLLOWUP,
    TouchPoint.Kind.TP7_EXPIRING,
    TouchPoint.Kind.TP8_EXPIRED,
)


def _create(lead, kind: str, anchor_dt) -> TouchPoint:
    return TouchPoint.objects.create(
        lead=lead, kind=kind, scheduled_for=anchor_dt + TEMPLATES[kind].offset
    )


def schedule_lead_created(lead) -> None:
    """Schedule the welcome (TP1) and lead-follow-up (TP2) touch-points for a new lead."""
    now = timezone.now()
    _create(lead, TouchPoint.Kind.TP1_WELCOME, now)
    _create(lead, TouchPoint.Kind.TP2_LEAD_FOLLOWUP, now)


def schedule_quote_sent(lead) -> None:
    """(Re)build the post-quote program from the lead's sent/expiry stamps."""
    cancel_pending(lead, kinds=_QUOTE_KINDS)
    sent = lead.quote_sent_at or timezone.now()
    _create(lead, TouchPoint.Kind.TP3_QUOTE_SENT_SMS, sent)
    _create(lead, TouchPoint.Kind.TP6_QUOTE_FOLLOWUP, sent)
    expires = lead.quote_expires_at
    if expires:
        if expires + TEMPLATES[TouchPoint.Kind.TP7_EXPIRING].offset > timezone.now():
            _create(lead, TouchPoint.Kind.TP7_EXPIRING, expires)
        _create(lead, TouchPoint.Kind.TP8_EXPIRED, expires)


def schedule_quote_viewed(lead) -> None:
    """Schedule the quote-viewed nudges (TP4/TP5) once per lead."""
    viewed_kinds = (TouchPoint.Kind.TP4_VIEWED_SMS, TouchPoint.Kind.TP5_VIEWED_EMAIL)
    if TouchPoint.objects.filter(lead=lead, kind__in=viewed_kinds).exists():
        return
    now = timezone.now()
    for kind in viewed_kinds:
        _create(lead, kind, now)


def schedule_review_request(lead) -> None:
    """Schedule the post-trip review request, idempotently."""
    kind = TouchPoint.Kind.REVIEW_REQUEST
    if TouchPoint.objects.filter(lead=lead, kind=kind).exists():
        return
    _create(lead, kind, timezone.now())


def cancel_pending(lead, *, kinds=None) -> int:
    """Cancel SCHEDULED touch-points for a lead.

    Defaults to every kind except review_request (so a booked lead still gets its
    post-trip review ask); pass explicit ``kinds`` to override, e.g. cancelling
    everything on LOST.
    """
    qs = TouchPoint.objects.filter(lead=lead, status=TouchPoint.Status.SCHEDULED)
    if kinds is None:
        qs = qs.exclude(kind=TouchPoint.Kind.REVIEW_REQUEST)
    else:
        qs = qs.filter(kind__in=kinds)
    return qs.update(status=TouchPoint.Status.CANCELLED)
