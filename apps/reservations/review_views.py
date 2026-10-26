"""The Trip Review screens (APC-59, design signed off 2026-09-26).

- the trip's review fragment (shared with Trip Processing, APC-102) and its JSON
  actions: save as you go, complete, log and resolve issues. The order-level list and
  order pages are retired; their URLs redirect to Trip Processing.

- money moves only from Trip Processing (APC-102), which locks what it settles.

Affiliate payables (APC-61) are worked in the pop-up's Driver pay pane through the
`payments` endpoints; they record, never move money.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.dispatch.models import Assignment
from apps.leads.models import Lead
from apps.payments import payables, trip_money
from apps.payments.forms import PayableInvoiceForm
from apps.payments.models import AffiliatePayable

from . import reviews
from .models import Reservation, Stop, TripIssue


class _Bad(Exception):
    pass


def _bad(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=400)


@login_required
@require_GET
def trip_review_list(request: HttpRequest) -> HttpResponse:
    """Retired (APC-102): Trip Processing replaces the order-level list. Old links and
    bookmarks land there; the open-issues drill-down keeps its filter."""
    url = reverse("processing")
    return redirect(f"{url}?issues=1" if request.GET.get("issues") == "1" else url)


@login_required
@require_GET
def trip_review_order(request: HttpRequest, lead_id: int) -> HttpResponse:
    """Retired (APC-102): an order's trips are processed one at a time; open its first."""
    from urllib.parse import urlencode

    from . import processing

    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=lead_id)
    trips = lead.reservations.order_by("pickup_date", "pickup_time", "pk")
    first = next((t for t in trips if processing.workable(t)), None)
    url = reverse("processing")
    if first is not None:
        return redirect(f"{url}?trip={first.pk}")
    return redirect(f"{url}?{urlencode({'q': lead.contact.name})}")


def _reviewable(pk: int) -> Reservation:
    """A trip the processing pane may work (APC-102, `processing.workable`): a booked order,
    not a plain cancellation, settled by status or over by the clock. No-shows and late
    cancels included — they carry the charge."""
    from . import processing

    trip = get_object_or_404(
        Reservation.objects.select_related("lead__contact", "service_type", "vehicle"), pk=pk
    )
    if not processing.workable(trip):
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
        "basis": f.basis,
        "grace_minutes": f.grace_minutes,
        "billable_minutes": f.billable_minutes,
        "waived": review.overtime_waived,
        "decided": review.decided_at is not None,
        "customer_amount": _money(f.customer_amount),
        "customer_overtime_gratuity": _money(f.customer_overtime_gratuity),
        "customer_overtime_total": _money(f.customer_overtime_total),
        "affiliate_share_pct": None
        if f.affiliate_share_pct is None
        else str(f.affiliate_share_pct),
        "affiliate_share_basis": f.affiliate_share_basis,
        "affiliate_overtime_derived": _money(f.affiliate_overtime_derived),
        "affiliate_overtime_amount": _money(f.affiliate_overtime_amount),
        "affiliate_overtime_overridden": f.affiliate_overtime_overridden,
        "override_note": review.affiliate_overtime_note,
        "expected_affiliate_amount": _money(f.expected_affiliate_amount),
        "driver_hourly_rate": _money(f.driver_hourly_rate),
        "driver_pay_amount": _money(f.driver_pay_amount),
        "rating": review.affiliate_rating,
        "complete": review.is_complete,
    }


@login_required
@require_GET
def trip_review_trip(request: HttpRequest, pk: int) -> HttpResponse:
    trip = _reviewable(pk)
    return render(request, "trip_review/_trip_review.html", trip_context(request, trip))


def trip_context(request: HttpRequest, trip: Reservation) -> dict:
    """Everything the trip's review fragment needs — the pop-up and the Trip Processing
    pane (APC-102) render the same one."""
    review = reviews.review_for(trip)
    coverage = _coverage(trip)
    payable = None
    if coverage is not None and not coverage.is_in_house:
        # One trip's payable, so the vendor invoice can be entered before Process.
        payables.ensure_payable(trip)
        payable = AffiliatePayable.objects.filter(assignment=coverage).first()
    stops = list(Stop.objects.filter(reservation=trip).order_by("sequence"))
    pickup, dropoff = review.actual_pickup_local, review.actual_dropoff_local
    zone_now = trip.pickup_at
    return {
        "trip": trip,
        "review": review,
        "coverage": coverage,
        "f": reviews.figures(review, coverage),
        "state": _review_json(review, coverage),
        "payable": payable,
        "payable_state": payables.payable_json(payable) if payable else None,
        "money": _money_json(trip),
        "card_on_file": bool(
            getattr(trip.lead, "payment", None) and trip.lead.payment.stripe_payment_method_id
        ),
        "invoice_form": PayableInvoiceForm(instance=payable) if payable else None,
        "can_pay": request.user.has_payments_access,
        "paid_methods": AffiliatePayable.Method.choices,
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
        "locked": review.is_processed,
    }


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
        if "override" in post:
            kwargs["affiliate_overtime_override"] = _decimal(
                post.get("override"), "The adjusted affiliate overtime"
            )
            kwargs["affiliate_overtime_note"] = post.get("override_note", "")
        if "rating" in post:
            kwargs["affiliate_rating"] = _int(post.get("rating"), "The rating")
        review = reviews.save_review(review, user=request.user, **kwargs)
    except (_Bad, reviews.ReviewError) as exc:
        return _bad(str(exc))
    payables.refresh_for_trip(trip)
    trip.refresh_from_db()
    return JsonResponse(
        {"ok": True, "review": _review_json(review, _coverage(trip)), "money": _money_json(trip)}
    )


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


# --- a trip's money (APC-101) ---------------------------------------------------------


def _money_json(trip) -> dict:
    m = trip_money.money_for(trip)
    return {
        "line_total": _money(m.line_total),
        "allocated": _money(m.allocated),
        "difference": _money(m.difference),
    }
