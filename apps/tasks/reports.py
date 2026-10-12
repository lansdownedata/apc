"""The two post-trip reports that go out by email (2026-10-04).

- **Customer review report** — every order that finished since the last report, and
  whether the customer was asked for a review, left one, and what they rated us on the trip
  page. It replaces the per-order "follow up on low feedback" task: low ratings are read
  here, at whatever cadence the cron runs (monthly).
- **Complaint report** — daily, and only on a day with new complaints: each complaint
  logged in a trip review since the last report, with the incident numbers around it.

Both measure "since the last report" from a stamp on `TaskConfig`, so nothing is missed or
repeated if a run is late or the schedule changes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import CustomerFeedback, Lead
from apps.messaging.models import Review, TouchPoint
from apps.notifications.email import send_html_email
from apps.reservations.models import Reservation, TripIssue

from . import post_trip
from .models import ReportedOrder, TaskConfig, format_local

logger = logging.getLogger(__name__)

# How far back the review report looks for finished orders it hasn't listed yet.
REVIEW_LOOKBACK = timedelta(days=31)
FIRST_COMPLAINT_WINDOW = timedelta(days=1)


def _url(name: str, *args) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{reverse(name, args=args)}"


def _send(template: str, subject: str, context: dict, config: TaskConfig) -> None:
    recipients = config.report_list
    if not recipients:
        logger.warning(
            "%s: nobody to send to — set report recipients in Settings → Tasks", template
        )
    for recipient in recipients:
        sent = send_html_email(
            to=recipient, subject=subject, template=template, context=context, internal=True
        )
        if not sent:
            logger.warning("%s: delivery to %s failed", template, recipient)


# --- customer review report -------------------------------------------------------------


def _asked(tp: TouchPoint | None) -> str:
    if tp is None:
        return "Not asked"
    if tp.status == TouchPoint.Status.SENT:
        return "Asked"
    if tp.status == TouchPoint.Status.CANCELLED and tp.error.startswith("Review request held"):
        return tp.error.replace("Review request held: ", "Held: ")
    if tp.status == TouchPoint.Status.SCHEDULED:
        return "Not sent yet"
    return f"Not sent ({tp.error or tp.get_status_display().lower()})"


def _review_said(review: Review | None) -> str:
    if review is None:
        return "No review"
    if review.rating:
        site = f" on {review.review_site}" if review.review_site else ""
        return f"{review.rating}-star review{site}"
    return "Opened the link, no review" if review.link_clicked else "No review yet"


def review_report_rows(since, until) -> list[dict]:
    """Booked orders whose last trip finished in (since, until] and that no earlier report
    listed, oldest first — each with its review request, the review that came back, and the
    trip-page rating. Six queries."""
    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    # ±2 days around the window: `pickup_date` is trip-local, the bounds are instants.
    window = (since.date() - timedelta(days=2), until.date() + timedelta(days=1))
    candidates = (
        Reservation.objects.filter(lead__status=Lead.Status.BOOKED, pickup_date__range=window)
        .exclude(lead__review_reported__isnull=False)
        .values("lead_id")
    )
    trips: dict[int, list[Reservation]] = defaultdict(list)
    for trip in Reservation.objects.filter(lead_id__in=candidates):
        trips[trip.lead_id].append(trip)

    finished = {}
    for lead_id, rows in trips.items():
        at = post_trip.order_finished_at(rows, now=until, grace=grace)
        if at is not None and since < at <= until:
            finished[lead_id] = at
    if not finished:
        return []

    leads = Lead.objects.filter(pk__in=finished).select_related("contact")
    asks = {
        tp.lead_id: tp
        for tp in TouchPoint.objects.filter(
            lead_id__in=finished, kind=TouchPoint.Kind.REVIEW_REQUEST
        ).order_by("pk")
    }
    reviews = {r.lead_id: r for r in Review.objects.filter(lead_id__in=finished).order_by("pk")}
    ratings = {f.lead_id: f for f in CustomerFeedback.objects.filter(lead_id__in=finished)}

    out = []
    for lead in leads:
        live = [t for t in trips[lead.pk] if not t.is_cancelled]
        last = max(live, key=lambda t: t.pickup_at or finished[lead.pk])
        zone = ZoneInfo(last.pickup_timezone or settings.TIME_ZONE)
        feedback = ratings.get(lead.pk)
        review = reviews.get(lead.pk)
        out.append(
            {
                "lead_id": lead.pk,
                "quote_no": lead.quote_no,
                "customer": lead.contact.name,
                "url": _url("order_detail", lead.pk),
                "trips": len(live),
                "finished_at": finished[lead.pk],
                "finished": format_local(finished[lead.pk].astimezone(zone), "M j"),
                "asked": _asked(asks.get(lead.pk)),
                "review": _review_said(review),
                "reviewed": bool(review and review.rating),
                "rating": feedback.rating if feedback else None,
                "comment": feedback.comment if feedback else "",
                "low": bool(feedback and feedback.is_low),
            }
        )
    return sorted(out, key=lambda r: (r["finished_at"], r["quote_no"]))


def send_review_report(now=None) -> int:
    """Email the customer review report for orders finished since the last one. Nothing
    goes out when no order finished. Returns the number of orders listed."""
    now = now or timezone.now()
    config = TaskConfig.load()
    since = config.review_report_through or now - REVIEW_LOOKBACK
    rows = review_report_rows(now - REVIEW_LOOKBACK, now)
    if rows:
        rated = [r["rating"] for r in rows if r["rating"] is not None]
        context = {
            "rows": rows,
            "low_rows": [r for r in rows if r["low"]],
            "since": timezone.localtime(since),
            "until": timezone.localtime(now),
            "orders": len(rows),
            "asked": sum(1 for r in rows if r["asked"] == "Asked"),
            "held": sum(1 for r in rows if r["asked"].startswith("Held")),
            "reviewed": sum(1 for r in rows if r["reviewed"]),
            "rated": len(rated),
            "average": sum(rated) / len(rated) if rated else None,
            "company_name": settings.COMPANY_NAME,
        }
        plural = "" if len(rows) == 1 else "s"
        subject = (
            f"Customer reviews: {len(rows)} order{plural} finished, {context['reviewed']} reviewed"
        )
        _send("review_report", subject, context, config)
        ReportedOrder.objects.bulk_create(
            [ReportedOrder(lead_id=r["lead_id"], reported_at=now) for r in rows],
            ignore_conflicts=True,
        )
    TaskConfig.objects.filter(pk=config.pk).update(review_report_through=now)
    return len(rows)


# --- complaint report -------------------------------------------------------------------


def complaint_rows(since, until) -> list[dict]:
    """Complaints logged in trip reviews in (since, until], oldest first. One query."""
    issues = (
        TripIssue.objects.filter(
            category=TripIssue.Category.COMPLAINT, created_at__gt=since, created_at__lte=until
        )
        .select_related(
            "review__reservation__lead__contact", "review__reservation__service_type", "created_by"
        )
        .prefetch_related("review__reservation__assignments__vendor")
        .prefetch_related("review__reservation__assignments__driver")
        .order_by("created_at", "pk")
    )
    out = []
    for issue in issues:
        trip = issue.review.reservation
        lead = trip.lead
        covered = next((a for a in trip.assignments.all() if a.status == a.Status.CONFIRMED), None)
        who = issue.created_by
        out.append(
            {
                "quote_no": lead.quote_no,
                "customer": lead.contact.name,
                "url": _url("trip_review_order", lead.pk),
                "trip": format_local(trip.pickup_at, "M j, g:i A") if trip.pickup_at else "",
                "covered_by": covered.provider_name if covered else "Not covered",
                "severity": issue.get_severity_display(),
                "note": issue.note,
                "logged_by": (who.get_full_name() or who.username) if who else "",
                "resolved": not issue.is_open,
            }
        )
    return out


def incident_stats(since, until) -> dict:
    """The numbers around the complaints: new issues by kind since the last report, what
    is still open, and complaints over the last 30 days. Two queries."""
    new = dict(
        TripIssue.objects.filter(created_at__gt=since, created_at__lte=until)
        .values_list("category")
        .annotate(n=Count("pk"))
    )
    totals = TripIssue.objects.aggregate(
        open_issues=Count("pk", filter=Q(resolved_at__isnull=True)),
        open_complaints=Count(
            "pk", filter=Q(resolved_at__isnull=True, category=TripIssue.Category.COMPLAINT)
        ),
        complaints_30d=Count(
            "pk",
            filter=Q(
                category=TripIssue.Category.COMPLAINT, created_at__gt=until - timedelta(days=30)
            ),
        ),
    )
    by_kind = [(label, new.get(value, 0)) for value, label in TripIssue.Category.choices]
    return {"new_by_kind": by_kind, **totals}


def send_complaint_report(now=None) -> int:
    """Email the day's new complaints with the incident numbers. Nothing goes out on a
    day without a new complaint. Returns the number of complaints listed."""
    now = now or timezone.now()
    config = TaskConfig.load()
    since = config.complaint_report_through or now - FIRST_COMPLAINT_WINDOW
    rows = complaint_rows(since, now)
    if rows:
        context = {
            "rows": rows,
            "stats": incident_stats(since, now),
            "since": timezone.localtime(since),
            "until": timezone.localtime(now),
            "company_name": settings.COMPANY_NAME,
        }
        plural = "" if len(rows) == 1 else "s"
        _send("complaint_report", f"{len(rows)} new customer complaint{plural}", context, config)
    TaskConfig.objects.filter(pk=config.pk).update(complaint_report_through=now)
    return len(rows)
