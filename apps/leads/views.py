"""Leads & Quotes — list, filter, and the quote/reservations detail view."""

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from apps.core.choices import Channel
from apps.payments import ledger

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
    }
    return render(request, "leads/lead_detail.html", context)
