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
from apps.core.phone import to_e164

from . import acknowledgements as ack
from .models import Reservation
from .services import confirm_trip_day, trip_day_group, trip_sheet_context


def _feedback_orders(trips) -> list[dict]:
    """APC-63 — the orders on this page that have finished running, each with its feedback
    so far. The form only shows for these."""
    from apps.leads.models import CustomerFeedback
    from apps.leads.services import order_finished

    leads = {t.lead_id: t.lead for t in trips}
    done = [lead for lead in leads.values() if order_finished(lead)]
    given = {f.lead_id: f for f in CustomerFeedback.objects.filter(lead__in=done)}
    return [{"lead": lead, "feedback": given.get(lead.pk)} for lead in done]


def _submit_feedback(request: HttpRequest, token: str, ctx: dict) -> HttpResponse:
    from apps.leads.services import FeedbackError, record_feedback

    # Only an order this token's page is showing, and only once it has finished.
    orders = {str(o["lead"].pk): o for o in ctx["feedback_orders"]}
    order = orders.get(request.POST.get("lead", ""))
    if order is None:
        return redirect("trip_confirm", token=token)
    try:
        record_feedback(
            order["lead"], rating=request.POST.get("rating"), comment=request.POST.get("comment")
        )
    except FeedbackError as exc:
        ctx["feedback_error"] = str(exc)
        return render(request, "public/trip_confirm.html", ctx)
    return redirect("trip_confirm", token=token)


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
    ctx = {
        "sheets": [trip_sheet_context(t) for t in trips],
        "contact_name": contact.name,
        "cancelled": not trips,
        "confirmed": bool(trips) and all(t.customer_confirmed_at is not None for t in trips),
        "ack_error": False,
        "feedback_orders": _feedback_orders(trips),
        "feedback_error": "",
    }
    # Once every order on the page has run, there's nothing left to confirm.
    ctx["all_finished"] = bool(trips) and len(ctx["feedback_orders"]) == len(
        {t.lead_id for t in trips}
    )
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

    lead = reservation.lead
    submitted = bool(lead.day_of_contact_name and lead.wedding_name)
    ctx = {
        "sheet": trip_sheet_context(reservation),
        "cancelled": reservation.is_cancelled,
        "submitted": submitted,
        "wedding_name": lead.wedding_name,
        "contact_name": lead.day_of_contact_name,
        "contact_phone": lead.day_of_contact_phone,
    }
    if request.method == "POST" and not ctx["cancelled"]:
        lead.wedding_name = (request.POST.get("wedding_name") or "").strip()[:200]
        lead.day_of_contact_name = (request.POST.get("contact_name") or "").strip()[:200]
        raw_phone = (request.POST.get("contact_phone") or "").strip()
        lead.day_of_contact_phone = to_e164(raw_phone) or raw_phone[:32]
        lead.save(
            update_fields=[
                "wedding_name",
                "day_of_contact_name",
                "day_of_contact_phone",
                "updated_at",
            ]
        )
        return redirect("wedding_details", token=token)
    return render(request, "public/wedding_details.html", ctx)
