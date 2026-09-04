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

from apps.core.phone import to_e164

from . import acknowledgements as ack
from .models import Reservation
from .services import trip_sheet_context


@require_http_methods(["GET", "POST"])
def trip_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """APC-19 — the customer confirms their trip details."""
    try:
        reservation = ack.read_trip_ack_token(token)
    except (BadSignature, Reservation.DoesNotExist):
        raise Http404 from None

    ctx = {
        "sheet": trip_sheet_context(reservation),
        "cancelled": reservation.is_cancelled,
        "confirmed": reservation.customer_confirmed_at is not None,
    }
    if request.method == "POST" and not ctx["cancelled"]:
        if reservation.customer_confirmed_at is None:
            reservation.customer_confirmed_at = timezone.now()
            reservation.save(update_fields=["customer_confirmed_at", "updated_at"])
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
