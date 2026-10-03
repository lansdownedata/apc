"""Server-rendered portal views — the authenticated web UI shell."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.leads.models import Lead
from apps.notifications.models import Notification, mark_all_read
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
        Notification.objects.unread_for(request.user)
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


@login_required
@require_POST
def notification_read(request, pk: int):
    """Open a notification from the tray: mark it read for this person, then go to it."""
    note = get_object_or_404(Notification.objects.for_user(request.user), pk=pk)
    note.mark_read_by(request.user)
    return redirect(note.url or "dashboard")


@login_required
@require_POST
def notifications_read_all(request):
    mark_all_read(request.user)
    back = request.META.get("HTTP_REFERER", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = "dashboard"
    return redirect(back)
