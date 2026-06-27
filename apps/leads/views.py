"""Leads & Quotes — list, filter, and the quote/reservations detail view."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.payments import ledger

from .forms import NewLeadForm
from .models import Lead


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
    }
    return render(request, "leads/lead_detail.html", context)


@login_required
@require_POST
def lead_update(request, pk):
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)
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
def lead_mark_lost(request, pk: int):
    lead = get_object_or_404(Lead, pk=pk)
    lead.status = Lead.Status.LOST
    lead.lost_reason = (request.POST.get("reason") or "").strip() or "Marked lost"
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_reopen(request, pk: int):
    lead = get_object_or_404(Lead, pk=pk)
    lead.status = Lead.Status.NEW
    lead.lost_reason = ""
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_create(request):
    form = NewLeadForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A customer name is required to create a lead.")
        return redirect("lead_list")
    cd = form.cleaned_data
    contact = Contact.objects.match_or_create(
        name=cd["name"], company=cd["company"], phone=cd["phone"],
        email=cd["email"], channel=cd["channel"],
    )
    lead = Lead.objects.create(
        contact=contact, channel=cd["channel"],
        assigned_agent=cd["agent"], status=Lead.Status.NEW,
    )
    return redirect("lead_detail", pk=lead.pk)
