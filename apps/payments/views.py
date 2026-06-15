import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from apps.leads.models import Lead

from . import ledger, webhooks


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
    return render(
        request,
        "orders/order_list.html",
        {"orders": orders, "filter": status_filter, "nav": "orders", "page_title": "Orders"},
    )
