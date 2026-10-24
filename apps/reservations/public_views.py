"""Public, token-keyed acknowledgement pages (APC-18 / APC-19 / APC-20).

No login — the signed token in the URL is the credential (see `acknowledgements.py`).
Each page is a plain GET render + a same-URL POST that stamps a timestamp / writes the
returned fields. Forged or stale tokens 404.
"""

from __future__ import annotations

from django.core.signing import BadSignature
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.contacts.models import Contact
from apps.core.net import client_ip

from . import acknowledgements as ack
from .models import Reservation
from .services import (
    confirm_trip_day,
    trip_day_group,
    trip_sheet_context,
    wedding_day_group,
)


def _feedback_orders(trips) -> list[dict]:
    """APC-63 — the orders on this page that have finished running, each with its feedback
    so far. The form only shows for these."""
    from apps.leads.models import CustomerFeedback
    from apps.leads.services import order_finished

    leads = {t.lead_id: t.lead for t in trips}
    done = [lead for lead in leads.values() if order_finished(lead)]
    given = {f.lead_id: f for f in CustomerFeedback.objects.filter(lead__in=done)}
    return [{"lead": lead, "feedback": given.get(lead.pk)} for lead in done]


def _submit_feedback(
    request: HttpRequest, token: str, ctx: dict, *, page: str = "trip_confirm"
) -> HttpResponse:
    from apps.leads.services import FeedbackError, record_feedback

    # Only an order this token's page is showing, and only once it has finished.
    orders = {str(o["lead"].pk): o for o in ctx["feedback_orders"]}
    order = orders.get(request.POST.get("lead", ""))
    if order is None:
        return redirect(page, token=token)
    try:
        record_feedback(
            order["lead"], rating=request.POST.get("rating"), comment=request.POST.get("comment")
        )
    except FeedbackError as exc:
        ctx["feedback_error"] = str(exc)
        return render(request, "public/trip_confirm.html", ctx)
    return redirect(page, token=token)


def _day_context(trips, contact_name: str) -> dict:
    ctx = {
        "sheets": [trip_sheet_context(t) for t in trips],
        "contact_name": contact_name,
        "cancelled": not trips,
        "confirmed": bool(trips) and all(t.customer_confirmed_at is not None for t in trips),
        "ack_error": False,
        "feedback_orders": _feedback_orders(trips),
        "feedback_error": "",
        "wedding": False,
        "trips_confirmed": False,
    }
    # Once every order on the page has run, there's nothing left to confirm.
    ctx["all_finished"] = bool(trips) and len(ctx["feedback_orders"]) == len(
        {t.lead_id for t in trips}
    )
    return ctx


@require_http_methods(["GET", "POST"])
def trip_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """APC-19 — the customer confirms every trip they have on one day.

    The token carries the customer and the date, so the trip set is resolved here rather
    than frozen when the notice went out. One checkbox covers the whole day; a day whose
    trips are all cancelled (or gone) has nothing to confirm and says so.
    """
    try:
        contact, day = ack.read_trip_day_ack_token(token)
    except (BadSignature, Contact.DoesNotExist):
        raise Http404 from None

    trips = trip_day_group(contact, day)
    ctx = _day_context(trips, contact.name)
    if request.method == "POST" and request.POST.get("form") == "feedback":
        return _submit_feedback(request, token, ctx)
    if request.method == "POST" and trips:
        if not request.POST.get("ack"):
            # `required` on the input is client-side only — refuse the bare POST.
            ctx["ack_error"] = True
            return render(request, "public/trip_confirm.html", ctx)
        confirm_trip_day(trips)
        return redirect("trip_confirm", token=token)
    return render(request, "public/trip_confirm.html", ctx)


@require_http_methods(["GET", "POST"])
def wedding_trip_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """APC-94 — a wedding's confirmation, sent to the billing contact. Same page as
    `trip_confirm`, scoped to the one order, plus the cleaning-fee / overtime
    acknowledgment. A day whose trips were confirmed before the fees existed still shows
    the acknowledgment (without the details box) so it can be given late."""
    from apps.leads import services as lead_services
    from apps.leads.models import Lead

    try:
        lead, day = ack.read_wedding_day_ack_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None

    trips = wedding_day_group(lead, day)
    ctx = _day_context(trips, lead.effective_billing_contact.name)
    fees_pending = lead.fees_acknowledged_at is None
    terms = lead_services.fee_terms_context()
    ctx.update(
        wedding=True,
        trips_confirmed=ctx["confirmed"],
        confirmed=ctx["confirmed"] and not fees_pending,
        fee=terms["fee"],
        fee_terms=lead_services.fee_terms_html(terms["config"]),
        fees_pending=fees_pending,
        fees_acknowledged_on=lead.fees_acknowledged_on,
        fee_ack_error=False,
    )
    if request.method == "POST" and request.POST.get("form") == "feedback":
        return _submit_feedback(request, token, ctx, page="wedding_trip_confirm")
    if request.method == "POST" and trips:
        boxes = ["ack_overtime"] + (["ack_cleaning"] if ctx["fee"] else [])
        ctx["ack_error"] = not ctx["trips_confirmed"] and not request.POST.get("ack")
        ctx["fee_ack_error"] = fees_pending and not all(request.POST.get(b) for b in boxes)
        if ctx["ack_error"] or ctx["fee_ack_error"]:
            return render(request, "public/trip_confirm.html", ctx)
        if fees_pending:
            lead_services.acknowledge_fees(lead, ip=client_ip(request))
        confirm_trip_day(trips)
        return redirect("wedding_trip_confirm", token=token)
    return render(request, "public/trip_confirm.html", ctx)


@require_http_methods(["GET", "POST"])
def affiliate_trip_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """APC-20 — the affiliate confirms they're covering the trip."""
    from django.conf import settings

    from apps.dispatch.models import Assignment

    try:
        assignment = ack.read_affiliate_ack_token(token)
    except (BadSignature, Assignment.DoesNotExist):
        raise Http404 from None

    reservation = assignment.reservation
    ctx = {
        "sheet": trip_sheet_context(reservation, affiliate=True),
        "cancelled": reservation.is_cancelled or not assignment.is_active,
        "confirmed": assignment.affiliate_confirmed_at is not None,
        "company_name": settings.COMPANY_NAME,
    }
    if request.method == "POST" and not ctx["cancelled"]:
        if assignment.affiliate_confirmed_at is None:
            assignment.affiliate_confirmed_at = timezone.now()
            assignment.save(update_fields=["affiliate_confirmed_at", "updated_at"])
        return redirect("affiliate_trip_confirm", token=token)
    return render(request, "public/affiliate_trip_confirm.html", ctx)


@require_http_methods(["GET", "POST"])
def wedding_details(request: HttpRequest, token: str) -> HttpResponse:
    """APC-18 — the couple returns their day-of point of contact + wedding name."""
    try:
        reservation = ack.read_wedding_details_token(token)
    except (BadSignature, Reservation.DoesNotExist):
        raise Http404 from None

    from apps.leads import contact_roles

    lead = reservation.lead
    # APC-64: the day-of contact is the order's day-of coordinator role.
    contact_name, contact_phone = contact_roles.day_of_contact(lead)
    ctx = {
        "sheet": trip_sheet_context(reservation),
        "cancelled": reservation.is_cancelled,
        "submitted": bool(contact_name and lead.wedding_name),
        "wedding_name": lead.wedding_name,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
    }
    if request.method == "POST" and not ctx["cancelled"]:
        lead.wedding_name = (request.POST.get("wedding_name") or "").strip()[:200]
        lead.save(update_fields=["wedding_name", "updated_at"])
        contact_roles.set_day_of_contact(
            lead,
            name=request.POST.get("contact_name") or "",
            phone=request.POST.get("contact_phone") or "",
        )
        return redirect("wedding_details", token=token)
    return render(request, "public/wedding_details.html", ctx)
