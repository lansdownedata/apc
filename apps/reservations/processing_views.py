"""Trip Processing screen (APC-102, spec 2026-10-25 §4): one list of finished trips grouped
by customer, the trip's pane beside it, and the actions that settle and lock a trip.

Reads are open to staff; settling money, locking and unlocking need payments access. The
pane is the trip review fragment (`review_views.trip_context`) with a processing footer.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import payment_access_required
from apps.payments import trip_money
from apps.payments.models import Charge

from . import editor as reservation_editor
from . import processing, review_views
from .models import Reservation, TripReview

# What an agent may set a trip's status to from here: the three that settle (spec §4.1).
_SETTABLE = (
    Reservation.TripStatus.DONE,
    Reservation.TripStatus.NO_SHOW,
    Reservation.TripStatus.LATE_CANCEL,
)


def _bad(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _trip(pk: int) -> Reservation:
    return get_object_or_404(
        Reservation.objects.select_related("lead__contact", "lead__payment", "service_type"),
        pk=pk,
    )


def _sender(request) -> str:
    return request.user.get_full_name() or request.user.username


def _sort(request, view: str) -> str:
    """The list's sort: the one asked for (and remembered for this user), else the one
    they chose last, else the view's default — oldest first on Ready (Moe 2026-10-25)."""
    options = processing.SORTS[view]
    user = request.user
    prefs = user.preferences if isinstance(user.preferences, dict) else {}
    mine = prefs.get("processing") if isinstance(prefs.get("processing"), dict) else {}
    key = f"{view}_sort"
    asked = request.GET.get("sort", "")
    if asked in options:
        if mine.get(key) != asked:
            user.preferences = {**prefs, "processing": {**mine, key: asked}}
            user.save(update_fields=["preferences"])
        return asked
    saved = mine.get(key)
    return saved if saved in options else next(iter(options))


@login_required
@require_GET
def processing_list(request: HttpRequest) -> HttpResponse:
    filters = processing.Filters.from_query(request.GET)
    sort = _sort(request, filters.view)
    if filters.view == "bills":
        return _bills(request, filters, sort)
    if filters.view == "processed":
        trips = processing.processed_trips(filters, sort)
        groups = []
    elif filters.issues:
        # The exceptions drill-down: every trip with an open issue, processed or not.
        trips = processing.issue_trips()
        groups = processing.group_by_customer(trips, sort)
    else:
        trips = processing.ready_trips(filters)
        groups = processing.group_by_customer(trips, sort)
    selected = None
    pane: dict = {}
    raw = request.GET.get("trip", "")
    if raw.isdigit():
        # A deep link (vendor page, reports) opens its trip whatever the filters say.
        selected = _trip(int(raw))
        if processing.workable(selected):
            pane = review_views.trip_context(request, selected)
            pane.update(_pane(request, selected))
        else:
            pane = {"unworkable": selected}
            selected = None
    return render(
        request,
        "processing/list.html",
        {
            "nav": "processing",
            "page_title": "Trip Processing",
            "filters": filters,
            "sort": sort,
            "sort_options": list(processing.SORTS[filters.view].items()),
            "groups": groups,
            "trips": trips,
            "selected": selected,
            "ready_count": len(trips) if filters.view == "ready" else None,
            "bills_count": processing.vendor_bill_count(),
            "processing": True,
            **pane,
            **reservation_editor.editor_context(request, None, []),
        },
    )


def _keep(filters) -> str:
    """The list's filters, carried on a vendor link so picking one doesn't drop them."""
    from urllib.parse import urlencode

    pairs = {
        "q": filters.q,
        "from": filters.date_from.isoformat() if filters.date_from else "",
        "to": filters.date_to.isoformat() if filters.date_to else "",
    }
    query = urlencode({k: v for k, v in pairs.items() if v})
    return f"&{query}" if query else ""


def _bills(request, filters, sort: str) -> HttpResponse:
    """Needs vendor bill (APC-104, spec §6): vendors on the left, the picked vendor's trips
    on the right, each with the trip pane's invoice form. Approve and Mark paid stay on the
    trip's pane: a trip leaves this view as soon as its invoice is saved."""
    from apps.payments.forms import PayableInvoiceForm
    from apps.vendors.models import Vendor

    groups = processing.vendor_bills(filters, sort)
    raw = request.GET.get("vendor", "")
    vendor = Vendor.objects.filter(pk=raw).first() if raw.isdigit() else None
    picked = next((g for g in groups if vendor and g.vendor.pk == vendor.pk), None)
    if picked is not None:
        for trip in picked.trips:
            trip.invoice_form = PayableInvoiceForm(
                instance=trip.payable, auto_id=f"bill{trip.payable.pk}_%s"
            )
    return render(
        request,
        "processing/list.html",
        {
            "nav": "processing",
            "page_title": "Trip Processing",
            "filters": filters,
            "sort": sort,
            "sort_options": list(processing.SORTS[filters.view].items()),
            "vendor_groups": groups,
            "keep": _keep(filters),
            "vendor": vendor,
            "picked": picked,
            # Filters on, the vendor has trips waiting: say the filters hid them, not that
            # nothing is owed.
            "filtered_out": bool(
                vendor and picked is None and processing.vendor_bill_trip_ids(vendor)
            ),
            "bills_count": processing.vendor_bill_count(),
            "can_pay": request.user.has_payments_access,
        },
    )


def _pane(request, trip) -> dict:
    """What the processing footer and history add to the review fragment."""
    review = TripReview.objects.select_related("processed_by", "unlocked_by").get(reservation=trip)
    money = trip_money.money_for(trip)
    plan = getattr(trip.lead, "payment", None)
    charges = list(
        Charge.objects.filter(reservation=trip)
        .exclude(status=Charge.Status.PENDING, stripe_client_secret="")
        .order_by("created_at")
    )
    from apps.payments import services as payments

    allocation = trip_money.allocation(trip.lead).get(trip.pk, 0)
    return {
        "review": review,
        "prepaid": allocation,
        "paid_for_trip": money.allocated,
        # What can still go back to the card; the rest is recorded as paid back another way.
        "card_refundable": payments.refundable_total(plan, trip=trip) if plan else 0,
        "refund_methods": [
            (value, label)
            for value, label in Charge.RefundMethod.choices
            if value != Charge.RefundMethod.CARD
        ],
        "needs_status": processing.needs_status(trip),
        "settable_statuses": [(s.value, s.label) for s in _SETTABLE],
        "difference": money.difference,
        "owed_back": -money.difference if money.difference < 0 else 0,
        "price_lines": trip.price_lines(),
        "card_on_file": bool(plan and plan.stripe_payment_method_id),
        "card_label": (
            f"{plan.card_brand.title()} ••{plan.card_last4}"
            if plan and plan.stripe_payment_method_id and plan.card_last4
            else ""
        ),
        "trip_charges": charges,
        "events": list(review.events.select_related("actor", "charge")),
    }


@login_required
@payment_access_required
@require_POST
def processing_process(request: HttpRequest, pk: int) -> JsonResponse:
    trip = _trip(pk)
    try:
        processing.process(
            trip,
            user=request.user,
            expected=request.POST.get("expected", ""),
            collect=request.POST.get("collect", ""),
            reason=request.POST.get("reason", ""),
            method=request.POST.get("method", "card"),
            reference=request.POST.get("reference", ""),
            base_url=request.build_absolute_uri("/")[:-1],
            sender_name=_sender(request),
        )
    except processing.ProcessingError as exc:
        return _bad(str(exc))
    except ArithmeticError:
        return _bad("The price changed; check the trip again.")
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def processing_unlock(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        processing.unlock(_trip(pk), user=request.user, reason=request.POST.get("reason", ""))
    except processing.ProcessingError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def processing_add_charge(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        processing.add_charge(
            _trip(pk),
            user=request.user,
            amount=request.POST.get("amount", ""),
            label=request.POST.get("label", ""),
            collect=request.POST.get("collect", "card"),
            base_url=request.build_absolute_uri("/")[:-1],
            sender_name=_sender(request),
        )
    except processing.ProcessingError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def processing_refund(request: HttpRequest, pk: int) -> JsonResponse:
    """A refund for any reason (spec §4.7, Moe 2026-10-25), processed or not: any amount up
    to what was paid for the trip, with a reason, back to the card or recorded as paid back
    another way; the trip gets a credit line for it, so what it owes doesn't move."""
    import stripe

    from apps.payments import services as payments

    from . import reviews
    from .models import ProcessingEvent

    trip = _trip(pk)
    if not processing.workable(trip):
        # A lost order is refunded through cancel-and-refund on the order page.
        return _bad("This trip can't be refunded here.")
    try:
        amount = processing.parse_amount(request.POST.get("amount", ""))
    except processing.ProcessingError as exc:
        return _bad(str(exc))
    review = reviews.review_for(trip)
    reason = request.POST.get("reason", "")
    try:
        refunded = trip_money.refund_trip(
            trip,
            amount,
            reason=reason,
            user=request.user,
            method=request.POST.get("method", "card"),
            reference=request.POST.get("reference", ""),
        )
    except payments.PaymentError as exc:
        return _bad(str(exc))
    except stripe.error.StripeError as exc:
        return _bad(getattr(exc, "user_message", None) or "Stripe refused it.", status=502)
    if not refunded:
        return _bad("That refund was just made.", status=409)
    ProcessingEvent.objects.create(
        review=review,
        kind=ProcessingEvent.Kind.REFUNDED,
        actor=request.user,
        note=reason.strip()[:255],
        amount=refunded,
    )
    return JsonResponse({"ok": True, "amount": f"{refunded:.2f}"})


@login_required
@require_POST
def processing_set_status(request: HttpRequest, pk: int) -> JsonResponse:
    """The parallel-run gap (spec §4.1): LA never sent Done. An agent says what happened."""
    from .services import set_trip_status

    trip = _trip(pk)
    status = request.POST.get("status", "")
    if status not in _SETTABLE:
        return _bad("Pick Done, No show or Late cancel.")
    if not processing.workable(trip) or processing.lock_message(trip):
        return _bad("This trip's status can't be set here.")
    set_trip_status(trip, status, user=request.user)
    return JsonResponse({"ok": True})
