"""The Trip Review screens (APC-59, design signed off 2026-09-26).

- the list of orders in review (`/portal/trip-review/`),
- one order in review — its trips and the final-billing figures,
- the trip review pop-up (a fragment) and its JSON actions: save as you go, complete,
  log and resolve issues.

- a trip's money (APC-101): charge its difference to the card on file, refund it, or send
  the pay link — per trip, for payments staff.

Affiliate payables (APC-61) are worked in the pop-up's Driver pay pane through the
`payments` endpoints; they record, never move money.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation

import stripe
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import payment_access_required
from apps.core import outbound
from apps.dispatch.models import Assignment
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.payments import payables, trip_money
from apps.payments import services as payment_services
from apps.payments.forms import PayableInvoiceForm
from apps.payments.models import AffiliatePayable
from apps.tasks.models import Task

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
    review = reviews.review_for(trip)
    coverage = _coverage(trip)
    payable = None
    if coverage is not None and not coverage.is_in_house:
        # Normally made when the order entered review; this covers one that entered
        # before payables existed.
        payables.ensure_payables(trip.lead)
        payable = AffiliatePayable.objects.filter(assignment=coverage).first()
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


# --- a trip's money (APC-101) ---------------------------------------------------------


def _money_json(trip) -> dict:
    m = trip_money.money_for(trip)
    return {
        "line_total": _money(m.line_total),
        "allocated": _money(m.allocated),
        "difference": _money(m.difference),
    }


def _settleable(pk: int) -> Reservation:
    """A trip whose money can be settled: on an order that entered review, any status but a
    plain cancellation — a no-show or a late cancel is exactly what gets charged or refunded."""
    trip = get_object_or_404(Reservation.objects.select_related("lead__contact"), pk=pk)
    in_review = Task.objects.filter(lead_id=trip.lead_id, kind="ops_review").exists()
    plain = (Reservation.TripStatus.CANCELLED, Reservation.TripStatus.COVID_CANCELLATION)
    if trip.trip_status in plain or not in_review:
        raise Http404
    return trip


@login_required
@payment_access_required
@require_POST
def trip_charge(request: HttpRequest, pk: int) -> JsonResponse:
    """Charge what the trip still owes (or `amount`) to the card on file."""
    trip = _settleable(pk)
    money = trip_money.money_for(trip)
    try:
        charge = trip_money.charge_trip(trip, money.difference, note=request.POST.get("note", ""))
    except (_Bad, payment_services.PaymentError) as exc:
        return _bad(str(exc))
    except stripe.error.StripeError as exc:
        return JsonResponse(
            {"ok": False, "error": getattr(exc, "user_message", None) or "Stripe refused it."},
            status=502,
        )
    return JsonResponse({"ok": True, "amount": f"{charge.amount:.2f}", "money": _money_json(trip)})


@login_required
@payment_access_required
@require_POST
def trip_refund(request: HttpRequest, pk: int) -> JsonResponse:
    """Refund the trip's overpayment (or `amount`), with a reason."""
    trip = _settleable(pk)
    money = trip_money.money_for(trip)
    try:
        refunded = trip_money.refund_trip(
            trip, -money.difference, reason=request.POST.get("reason", ""), user=request.user
        )
    except (_Bad, payment_services.PaymentError) as exc:
        return _bad(str(exc))
    except stripe.error.StripeError as exc:
        return JsonResponse(
            {"ok": False, "error": getattr(exc, "user_message", None) or "Stripe refused it."},
            status=502,
        )
    return JsonResponse({"ok": True, "amount": f"{refunded:.2f}", "money": _money_json(trip)})


@login_required
@payment_access_required
@require_POST
def trip_pay_link(request: HttpRequest, pk: int) -> JsonResponse:
    """Open a pay-page intent for what the trip owes and text or email the customer."""
    trip = _settleable(pk)
    money = trip_money.money_for(trip)
    if not outbound.enabled():
        return _bad(outbound.OFF_MESSAGE)  # before an intent is opened, not after
    try:
        trip_money.open_trip_intent(trip, money.difference, note=request.POST.get("note", ""))
        channel = payment_services.send_pay_link(
            trip.lead,
            base_url=request.build_absolute_uri("/")[:-1],
            sender_name=request.user.get_full_name() or request.user.username,
        )
    except (_Bad, payment_services.PaymentError) as exc:
        return _bad(str(exc))
    except stripe.error.StripeError as exc:
        return JsonResponse(
            {"ok": False, "error": getattr(exc, "user_message", None) or "Stripe refused it."},
            status=502,
        )
    except (PodiumAPIError, PodiumNotConnected) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    return JsonResponse({"ok": True, "channel": channel, "money": _money_json(trip)})
