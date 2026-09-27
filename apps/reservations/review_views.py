"""The Trip Review screens (APC-59, design signed off 2026-09-26).

- the list of orders in review (`/portal/trip-review/`),
- one order in review — its trips and the final-billing figures,
- the trip review pop-up (a fragment) and its JSON actions: save as you go, complete,
  log and resolve issues.

Money moves nowhere from here: the charge buttons (APC-60) and affiliate payables
(APC-61) arrive with their own tickets.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.dispatch.models import Assignment
from apps.tasks.models import Task, TaskConfig

from . import review_queue, reviews
from .models import Reservation, Stop, TripIssue

_STAGES = [
    (review_queue.Stage.NEEDS_REVIEW, "Needs ops review"),
    (review_queue.Stage.BILLING, "In billing"),
    (review_queue.Stage.CLOSED, "Closed"),
]
_BALANCES = [("owed", "Still owed"), ("settled", "Settled")]
_WINDOWS = [
    ("7", "Last 7 days"),
    ("30", "Last 30 days"),
    ("90", "Last 90 days"),
    ("all", "Any time"),
]


class _Bad(Exception):
    pass


def _bad(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=400)


@login_required
@require_GET
def trip_review_list(request: HttpRequest) -> HttpResponse:
    filters = review_queue.Filters.from_query(request.GET)
    rows, counts = review_queue.list_page(filters)
    return render(
        request,
        "trip_review/list.html",
        {
            "nav": "trip_review",
            "page_title": "Trip Review",
            "rows": rows,
            "counts": counts,
            "filters": filters,
            "stage_options": _STAGES,
            "balance_options": _BALANCES,
            "window_options": _WINDOWS,
        },
    )


@login_required
@require_GET
def trip_review_order(request: HttpRequest, lead_id: int) -> HttpResponse:
    row = review_queue.order_review(lead_id)
    if row is None:
        raise Http404
    return render(
        request,
        "trip_review/order.html",
        {"nav": "trip_review", "page_title": row.lead.quote_no, "row": row},
    )


def _reviewable(pk: int) -> Reservation:
    """A live trip on an order that has entered review; anything else has nothing to do."""
    trip = get_object_or_404(
        Reservation.objects.select_related("lead__contact", "service_type", "vehicle"), pk=pk
    )
    in_review = Task.objects.filter(lead_id=trip.lead_id, kind="ops_review").exists()
    if trip.is_cancelled or not in_review:
        raise Http404
    return trip


def _coverage(trip: Reservation):
    return (
        Assignment.objects.active()
        .filter(reservation=trip)
        .select_related("vendor", "driver")
        .first()
    )


def _money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def _review_json(review, coverage) -> dict:
    f = reviews.figures(review, coverage)
    return {
        "billed_minutes": f.billed_minutes,
        "actual_minutes": f.actual_minutes,
        "over_minutes": f.over_minutes,
        "suggested_minutes": f.suggested_minutes,
        "rule": f.rule,
        "billable_minutes": f.billable_minutes,
        "waived": review.overtime_waived,
        "decided": review.decided_at is not None,
        "customer_amount": _money(f.customer_amount),
        "driver_minutes": f.driver_minutes,
        "affiliate_rate": _money(f.affiliate_rate),
        "affiliate_overtime_amount": _money(f.affiliate_overtime_amount),
        "expected_affiliate_amount": _money(f.expected_affiliate_amount),
        "rating": review.affiliate_rating,
        "complete": review.is_complete,
    }


@login_required
@require_GET
def trip_review_trip(request: HttpRequest, pk: int) -> HttpResponse:
    trip = _reviewable(pk)
    review = reviews.review_for(trip)
    coverage = _coverage(trip)
    stops = list(Stop.objects.filter(reservation=trip).order_by("sequence"))
    pickup, dropoff = review.actual_pickup_local, review.actual_dropoff_local
    zone_now = trip.pickup_at
    return render(
        request,
        "trip_review/_trip_review.html",
        {
            "trip": trip,
            "review": review,
            "coverage": coverage,
            "f": reviews.figures(review, coverage),
            "state": _review_json(review, coverage),
            "config": TaskConfig.load(),
            "stops": stops,
            "issues": list(
                review.issues.select_related("created_by").order_by("resolved_at", "-created_at")
            ),
            "tz_abbr": zone_now.tzname() if zone_now else "",
            "times": {
                "pickup_date": pickup.date().isoformat() if pickup else "",
                "pickup_time": pickup.strftime("%H:%M") if pickup else "",
                "dropoff_date": dropoff.date().isoformat() if dropoff else "",
                "dropoff_time": dropoff.strftime("%H:%M") if dropoff else "",
            },
            "categories": TripIssue.Category.choices,
            "severities": TripIssue.Severity.choices,
        },
    )


def _local(trip, post, prefix: str):
    day, clock = (
        (post.get(f"{prefix}_date") or "").strip(),
        (post.get(f"{prefix}_time") or "").strip(),
    )
    if not day and not clock:
        return None
    if not (day and clock):
        raise _Bad(f"Enter both the date and the time for the {prefix.replace('_', '-')}.")
    try:
        return reviews.from_local(trip, date.fromisoformat(day), time.fromisoformat(clock))
    except ValueError:
        raise _Bad("That date or time isn't valid.") from None


def _int(value: str, label: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise _Bad(f"{label} has to be a whole number.") from None


def _decimal(value: str, label: str) -> Decimal | None:
    value = (value or "").replace("$", "").replace(",", "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise _Bad(f"{label} has to be an amount.") from None


@login_required
@require_POST
def trip_review_save(request: HttpRequest, pk: int) -> JsonResponse:
    """Save as you go. Only the fields posted change — the pop-up sends one group at a
    time (times, the customer decision, driver pay, the rating), so touching the times
    never counts as deciding the overtime."""
    trip = _reviewable(pk)
    review = reviews.review_for(trip)
    post = request.POST
    kwargs: dict = {}
    try:
        for prefix in ("pickup", "dropoff"):
            if f"{prefix}_date" in post or f"{prefix}_time" in post:
                kwargs[f"actual_{prefix}_at"] = _local(trip, post, prefix)
        if "billable_minutes" in post or "waived" in post:
            kwargs["billable_overtime_minutes"] = (
                _int(post.get("billable_minutes"), "Billable minutes") or 0
            )
            kwargs["overtime_waived"] = post.get("waived") == "1"
            kwargs["waive_reason"] = post.get("waive_reason", "")
        if "driver_minutes" in post:
            kwargs["driver_overtime_minutes"] = _int(post.get("driver_minutes"), "Driver minutes")
        if "affiliate_rate" in post:
            kwargs["affiliate_rate"] = _decimal(post.get("affiliate_rate"), "The affiliate rate")
        if "rating" in post:
            kwargs["affiliate_rating"] = _int(post.get("rating"), "The rating")
        review = reviews.save_review(review, user=request.user, **kwargs)
    except (_Bad, reviews.ReviewError) as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True, "review": _review_json(review, _coverage(trip))})


@login_required
@require_POST
def trip_review_complete(request: HttpRequest, pk: int) -> JsonResponse:
    trip = _reviewable(pk)
    try:
        reviews.complete_review(reviews.review_for(trip), user=request.user)
    except reviews.ReviewError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True})


@login_required
@require_POST
def trip_review_issue_add(request: HttpRequest, pk: int) -> JsonResponse:
    trip = _reviewable(pk)
    try:
        issue = reviews.add_issue(
            reviews.review_for(trip),
            category=request.POST.get("category", ""),
            severity=request.POST.get("severity", ""),
            note=request.POST.get("note", ""),
            user=request.user,
        )
    except reviews.ReviewError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True, "id": issue.pk})


@login_required
@require_POST
def trip_review_issue_resolve(request: HttpRequest, pk: int) -> JsonResponse:
    issue = get_object_or_404(TripIssue, pk=pk)
    reviews.resolve_issue(issue, user=request.user)
    return JsonResponse({"ok": True})
