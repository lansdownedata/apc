"""Server-rendered portal views — the authenticated web UI shell."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import reports as payment_reports


@login_required
def dashboard(request):
    leads = Lead.objects.all()
    counts = {
        "new": leads.filter(status=Lead.Status.NEW).count(),
        "quoted": leads.filter(status=Lead.Status.QUOTED).count(),
        "booked": leads.filter(status=Lead.Status.BOOKED).count(),
        "lost": leads.filter(status=Lead.Status.LOST).count(),
    }
    recent = (
        Lead.objects.select_related("contact", "assigned_agent")
        .prefetch_related("reservations")
        .order_by("-created_at")[:8]
    )
    alerts = (
        Notification.objects.unread()
        .select_related("lead", "lead__contact")
        .order_by("-created_at")[:6]
    )
    context = {
        "nav": "dashboard",
        "page_title": "Dashboard",
        "counts": counts,
        # Orders holding authorized money that nobody has confirmed yet (APC-26). The
        # landing page's job is to say what needs a human *today*, and this is the only
        # queue with a deadline the card networks enforce for us.
        "awaiting": payment_reports.awaiting_confirmation_summary(),
        "open_pipeline": Lead.objects.open_pipeline_value(),
        "recent_leads": recent,
        "alerts": alerts,
    }
    return render(request, "portal/dashboard.html", context)
