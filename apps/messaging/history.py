"""The message history on an order: what was sent, what is waiting, and why.

One row per Delivery, plus one per TouchPoint that has none (scheduled, waiting, skipped
for lack of a recipient, cancelled). Spec §9.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings

from .models import Delivery, TouchPoint

_PROBLEM, _UPCOMING, _DONE, _CANCELLED = 0, 1, 2, 3

_DELIVERY_STATUS = {
    Delivery.Status.SENT: "Sent",
    Delivery.Status.FAILED: "Failed",
    Delivery.Status.SKIPPED: "Skipped",
    Delivery.Status.QUEUED: "Waiting",
}


@dataclass(frozen=True)
class HistoryRow:
    name: str
    when: datetime
    trip_label: str
    to: str
    status: str
    reason: str
    when_display: str = ""
    group: int = _DONE


def _when_display(when: datetime, zone_name: str) -> str:
    local = when.astimezone(ZoneInfo(zone_name or settings.TIME_ZONE))
    stamp = f"{local:%b} {local.day}, {local:%-I:%M %p}"
    return f"{stamp} {local.tzname()}"


def _trip_label(res) -> str:
    if res is None:
        return ""
    day = f"{res.pickup_date:%b} {res.pickup_date.day}" if res.pickup_date else ""
    return f"{res.service_label} · {day}" if day else res.service_label


def _group(status: str) -> int:
    if status in ("Failed", "Waiting", "Skipped"):
        return _PROBLEM
    if status == "Cancelled":
        return _CANCELLED
    return _UPCOMING if status == "Scheduled" else _DONE


def _tp_status(tp: TouchPoint) -> tuple[str, str]:
    S = TouchPoint.Status
    if tp.status == S.SCHEDULED:
        return ("Waiting", tp.error) if tp.error else ("Scheduled", "")
    if tp.status == S.SKIPPED:
        return "Skipped", tp.error or "No recipient"
    if tp.status == S.CANCELLED:
        return "Cancelled", tp.error
    return ("Failed" if tp.status == S.FAILED else "Sent"), tp.error


def for_lead(lead) -> list[HistoryRow]:
    """History rows for an order: problems, then upcoming by time, then sent newest first."""
    touchpoints = (
        lead.touchpoints.select_related("message", "reservation__service_type")
        .prefetch_related("deliveries")
        .order_by("id")
    )
    rows: list[tuple[datetime, HistoryRow]] = []
    for tp in touchpoints:
        res = tp.reservation
        zone = res.pickup_timezone if res is not None else ""
        name = tp.message.name if tp.message_id else tp.get_kind_display()
        label = _trip_label(res)
        deliveries = list(tp.deliveries.all())
        if not deliveries:
            status, reason = _tp_status(tp)
            when = tp.sent_at or tp.scheduled_for or tp.created_at
            row = HistoryRow(
                name, when, label, "", status, reason, _when_display(when, zone), _group(status)
            )
            # A cancelled send sorts by when it was cancelled, not by the time it was due.
            rows.append((tp.updated_at if status == "Cancelled" else when, row))
            continue
        for d in deliveries:
            status = _DELIVERY_STATUS.get(d.status, d.get_status_display())
            if d.status == Delivery.Status.SENT:
                when = tp.sent_at or d.updated_at
            elif d.status in (Delivery.Status.FAILED, Delivery.Status.SKIPPED):
                when = d.updated_at
            else:
                when = tp.scheduled_for or d.created_at
            who = d.recipient_label or d.get_channel_display()
            to = f"{who} · {d.get_channel_display()}" if d.recipient_label else who
            row = HistoryRow(
                name, when, label, to, status, d.reason, _when_display(when, zone), _group(status)
            )
            rows.append((when, row))
    # Problems and upcoming sends oldest first; sent, then cancelled, newest first.
    rows.sort(key=lambda r: (r[1].group, r[0].timestamp() * (-1 if r[1].group >= _DONE else 1)))
    return [row for _, row in rows]
