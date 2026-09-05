"""Scheduled jobs over reservations — triggered by the HTTP cron endpoints."""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.notifications.email import send_html_email

from . import reports

logger = logging.getLogger(__name__)


def send_unconfirmed_trip_report(today=None) -> int:
    """Email the office tomorrow's unconfirmed trips. Returns the number of customers listed.

    Nothing goes out on a quiet day — an empty report trains people to ignore it.
    """
    rows = reports.unconfirmed_trip_rows(today=today)
    if not rows:
        return 0
    count = len(rows)
    trip_count = sum(len(row["trips"]) for row in rows)
    context = {
        "rows": rows,
        "today": today or timezone.localdate(),
        "trip_count": trip_count,
        "company_name": settings.COMPANY_NAME,
    }
    plural = "s" if count != 1 else ""
    subject = f"Unconfirmed trips tomorrow — {count} customer{plural}, {trip_count} trips"
    if not settings.TRIP_CONFIRM_REPORT_EMAILS:
        logger.warning(
            "unconfirmed-trips report: %d customer(s) listed but "
            "TRIP_CONFIRM_REPORT_EMAILS is empty — nothing sent",
            count,
        )
    for recipient in settings.TRIP_CONFIRM_REPORT_EMAILS:
        sent = send_html_email(
            to=recipient, subject=subject, template="unconfirmed_trips", context=context
        )
        if not sent:
            logger.warning("unconfirmed-trips report: delivery to %s failed", recipient)
    return count
