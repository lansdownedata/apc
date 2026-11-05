"""Scheduled jobs over reservations — triggered by the HTTP cron endpoints."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from . import reports


def send_unconfirmed_trip_report(today=None) -> int:
    """Email the office tomorrow's unconfirmed trips through the "Unconfirmed trips report"
    message row. Returns the number of customers listed.

    Nothing goes out on a quiet day — an empty report trains people to ignore it.
    """
    from apps.messaging import rendering, sender

    rows = reports.unconfirmed_trip_rows(today=today)
    if not rows:
        return 0
    count = len(rows)
    trip_count = sum(len(row["trips"]) for row in rows)
    today = today or timezone.localdate()
    context = {
        "rows": rows,
        "today": today,
        "trip_count": trip_count,
        "company_name": settings.COMPANY_NAME,
    }
    sender.send_report(
        "unconfirmed_trips_report",
        "unconfirmed_trips",
        context,
        {
            "report_date": rendering.format_date(today, "l, F j, Y"),
            "customers": rendering.counted(count, "customer"),
            "trips": rendering.counted(trip_count, "trip"),
            "trip_count": str(trip_count),
        },
    )
    return count
