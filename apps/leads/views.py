"""Leads & Quotes — list, filter, and the quote/reservations detail view."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.payments import ledger

from . import services
from .forms import NewLeadForm
from .models import Lead, Vehicle


def _reservation_draft(r) -> dict:
    return {
        "id": r.pk,
        "tripType": r.trip_type,
        "service": r.service,
        "date": r.pickup_date.isoformat() if r.pickup_date else "",
        "time": r.pickup_time.strftime("%H:%M") if r.pickup_time else "",
        "vehicle": r.vehicle_id or "",
        "pax": r.passengers,
        "baseRate": float(r.base_rate),
        "hours": float(r.hours),
        "hourlyRate": float(r.hourly_rate),
        "minHours": float(r.min_hours),
        "stops": [{"address": s.address, "note": s.note} for s in r.stops.all()],
    }


@login_required
def lead_list(request):
    leads = (
        Lead.objects.select_related("contact", "assigned_agent")
        .prefetch_related("reservations")
        .order_by("-created_at")
    )

    status = request.GET.get("status", "").strip().lower()
    if status in Lead.Status.values:
        leads = leads.filter(status=status)

    channel = request.GET.get("channel", "").strip().lower()
    if channel in Channel.values:
        leads = leads.filter(channel=channel)

    query = request.GET.get("q", "").strip()
    if query:
        leads = leads.filter(
            Q(contact__name__icontains=query)
            | Q(contact__company__icontains=query)
            | Q(contact__email__icontains=query)
            | Q(reservations__service__icontains=query)
        ).distinct()

    everything = Lead.objects.all()
    counts = {
        "all": everything.count(),
        "new": everything.filter(status=Lead.Status.NEW).count(),
        "quoted": everything.filter(status=Lead.Status.QUOTED).count(),
        "booked": everything.filter(status=Lead.Status.BOOKED).count(),
        "lost": everything.filter(status=Lead.Status.LOST).count(),
    }

    context = {
        "nav": "leads",
        "page_title": "Leads & Quotes",
        "leads": leads,
        "counts": counts,
        "open_pipeline": Lead.objects.open_pipeline_value(),
        "status_filter": status or "all",
        "channel_filter": channel or "",
        "q": query,
        "channels": Channel.choices,
        "agent_options": [
            (u.pk, u.get_full_name() or u.username)
            for u in User.objects.order_by("first_name", "username")
        ],
    }
    return render(request, "leads/lead_list.html", context)


@login_required
def lead_detail(request, pk):
    lead = get_object_or_404(
        Lead.objects.select_related("contact", "assigned_agent").prefetch_related(
            "reservations__vehicle",
            "reservations__stops",
            "notifications",
        ),
        pk=pk,
    )
    _vehicles = list(Vehicle.objects.filter(active=True).order_by("name").values("id", "name"))
    context = {
        "nav": "leads",
        "page_title": lead.quote_no,
        "lead": lead,
        "reservations": lead.reservations.all(),
        "payment": getattr(lead, "payment", None),
        "balances": ledger.order_balances(lead),
        "ledger_entries": lead.journal_entries.prefetch_related("lines").order_by(
            "posted_at", "id"
        ),
        "charges": [c for p in [getattr(lead, "payment", None)] if p for c in p.charges.all()],
        "channels": Channel.choices,
        "agents": [
            (u.pk, u.get_full_name() or u.username)
            for u in User.objects.order_by("first_name", "username")
        ],
        "reservations_json": [_reservation_draft(r) for r in lead.reservations.all()],
        "vehicles_json": _vehicles,
        "vehicle_options": [(v["id"], v["name"]) for v in _vehicles],
    }
    return render(request, "leads/lead_detail.html", context)


@login_required
@require_POST
def lead_update(request, pk: int) -> JsonResponse:
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)

    # Validate before writing anything.
    if "name" in request.POST and not request.POST.get("name", "").strip():
        return JsonResponse({"ok": False, "error": "Name cannot be blank."}, status=400)
    if "email" in request.POST:
        email_val = request.POST.get("email", "").strip()
        if email_val:
            try:
                validate_email(email_val)
            except ValidationError:
                return JsonResponse(
                    {"ok": False, "error": "Enter a valid email address."}, status=400
                )

    contact = lead.contact
    contact_fields = []
    for field in ("name", "phone", "email", "company"):
        if field in request.POST:
            setattr(contact, field, request.POST.get(field, "").strip())
            contact_fields.append(field)
    if contact_fields:
        contact.save(update_fields=contact_fields + ["updated_at"])

    lead_fields = []
    channel = request.POST.get("channel")
    if channel in Channel.values:
        lead.channel = channel
        lead_fields.append("channel")
    if "agent" in request.POST:
        agent_id = request.POST.get("agent") or None
        lead.assigned_agent_id = int(agent_id) if agent_id else None
        lead_fields.append("assigned_agent")
    if lead_fields:
        lead.save(update_fields=lead_fields + ["updated_at"])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def lead_mark_lost(request, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead, pk=pk)
    lead.status = Lead.Status.LOST
    lead.lost_reason = (request.POST.get("reason") or "").strip() or "Marked lost"
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_reopen(request, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead, pk=pk)
    lead.status = Lead.Status.NEW
    lead.lost_reason = ""
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_send_quote(request, pk: int) -> JsonResponse:
    """Create/refresh the deposit plan, build the Stripe link, transition the lead,
    and email it over Podium. Returns the send result as JSON."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)
    token = services.make_deposit_token(lead)
    success_url = request.build_absolute_uri(reverse("quote_deposit_success", args=[token]))
    cancel_url = request.build_absolute_uri(reverse("quote_deposit_cancel", args=[token]))
    result = services.send_quote(lead, success_url=success_url, cancel_url=cancel_url)
    return JsonResponse(result.as_dict(), status=result.http_status)


def quote_deposit_success(request, token: str):  # implemented in Task 4
    raise Http404


def quote_deposit_cancel(request, token: str):  # implemented in Task 4
    raise Http404


@login_required
@require_POST
def lead_create(request) -> HttpResponse:
    form = NewLeadForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "; ".join(f"{k}: {e[0]}" for k, e in form.errors.items()) or "Could not create lead.",
        )
        return redirect("lead_list")
    cd = form.cleaned_data
    contact = Contact.objects.match_or_create(
        name=cd["name"],
        company=cd["company"],
        phone=cd["phone"],
        email=cd["email"],
        channel=cd["channel"],
    )
    lead = Lead.objects.create(
        contact=contact,
        channel=cd["channel"],
        assigned_agent=cd["agent"],
        status=Lead.Status.NEW,
    )
    return redirect("lead_detail", pk=lead.pk)
