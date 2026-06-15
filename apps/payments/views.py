from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.permissions import payment_access_required
from apps.leads.models import Lead
from apps.reservations.models import Reservation

from . import ledger, services, webhooks


@csrf_exempt
def stripe_webhook(request):
    """Verify the Stripe signature, then reconcile the event."""
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(request.body, sig, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponseBadRequest("Invalid signature.")
    webhooks.process_stripe_event(event)
    return HttpResponse(status=200)


@login_required
def orders_list(request):
    leads = (
        Lead.objects.filter(status=Lead.Status.BOOKED)
        .select_related("contact", "payment")
        .prefetch_related("reservations")
        .order_by("-created_at")
    )
    status_filter = request.GET.get("filter", "all")
    orders = []
    for lead in leads:
        bals = ledger.order_balances(lead)
        plan = getattr(lead, "payment", None)
        orders.append({"lead": lead, "balances": bals, "plan": plan})
    if status_filter == "failed":
        orders = [o for o in orders if o["plan"] and o["plan"].balance_status == "failed"]
    elif status_filter == "ar":
        orders = [o for o in orders if o["balances"]["ar"] > 0]
    elif status_filter == "recognized":
        orders = [
            o for o in orders if o["balances"]["deferred"] == 0 and o["balances"]["recognized"] > 0
        ]
    summary = {
        key: sum((o["balances"][key] for o in orders), Decimal("0.00"))
        for key in ("collected", "deferred", "recognized", "ar", "refunded")
    }
    return render(
        request,
        "orders/order_list.html",
        {
            "orders": orders,
            "filter": status_filter,
            "nav": "orders",
            "page_title": "Orders",
            "summary": summary,
        },
    )


def _plan(lead_id):
    lead = get_object_or_404(Lead, pk=lead_id)
    return lead, getattr(lead, "payment", None)


@login_required
@payment_access_required
@require_POST
def order_refund(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan:
        try:
            amount = Decimal(request.POST.get("amount") or plan.quote_total)
        except (InvalidOperation, TypeError):
            messages.error(request, "Enter a valid refund amount.")
            return redirect("lead_detail", pk=lead_id)
        refunded = services.refund_payment(plan, amount)
        messages.success(request, f"Refunded ${refunded}.")
    return redirect("lead_detail", pk=lead_id)


@login_required
@payment_access_required
@require_POST
def order_cancel_refund(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan:
        services.refund_payment(plan, plan.quote_total)
    lead.reservations.update(
        trip_status=Reservation.TripStatus.CANCELLED,
        revenue_status=Reservation.RevenueStatus.REVERSED,
    )
    lead.status = Lead.Status.LOST
    lead.lost_reason = "Cancelled"
    lead.has_alert = False
    lead.save(update_fields=["status", "lost_reason", "has_alert", "updated_at"])
    messages.success(request, "Order cancelled and refunded.")
    return redirect("lead_detail", pk=lead_id)


@login_required
@payment_access_required
@require_POST
def order_retry_balance(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan:
        services.charge_balance(plan)
    return redirect("lead_detail", pk=lead_id)


@login_required
@payment_access_required
@require_POST
def order_mark_paid(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan and request.POST.get("amount"):
        try:
            amount = Decimal(request.POST["amount"])
        except (InvalidOperation, TypeError):
            messages.error(request, "Enter a valid amount.")
            return redirect("lead_detail", pk=lead_id)
        services.mark_paid_offline(plan, amount)
        messages.success(request, "Offline payment recorded.")
    return redirect("lead_detail", pk=lead_id)
