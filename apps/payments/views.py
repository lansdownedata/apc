from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.permissions import payment_access_required
from apps.core.choices import Channel
from apps.dispatch import services as dispatch_services
from apps.integrations import podium
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads import services as lead_services
from apps.leads.models import Lead
from apps.messaging import services as messaging_services
from apps.messaging.models import Message
from apps.reservations.models import Reservation

from . import ledger, reports, services, webhooks


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
    # The engaged queue is a different shape from the finance table — a time-boxed
    # worklist, not a money ledger — so it renders its own rows rather than being squeezed
    # into nine columns of totals.
    awaiting = reports.awaiting_confirmation_rows()
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
            "awaiting": awaiting,
            "awaiting_summary": reports.awaiting_confirmation_summary(rows=awaiting),
            "filter": status_filter,
            "nav": "orders",
            "page_title": "Orders",
            "summary": summary,
            "channels": Channel.choices,
            "agent_options": lead_services.agent_options(),
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
    if lead.status != Lead.Status.BOOKED:
        messages.error(request, "Only booked orders can be cancelled.")
        return redirect("lead_detail", pk=lead_id)
    if plan:
        services.refund_payment(plan, plan.quote_total)
    lead.reservations.update(
        trip_status=Reservation.TripStatus.CANCELLED,
        revenue_status=Reservation.RevenueStatus.REVERSED,
    )
    # Cancelled trips drop off the dispatch board, so any affiliate offer still open on
    # them would be unreachable — pull it here while we still know the trip died.
    dispatch_services.release_trips(lead.reservations.all(), note="Order cancelled")
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


def _json_error(message: str, status: int = 400):
    return JsonResponse({"ok": False, "error": message}, status=status)


@login_required
@payment_access_required
@require_POST
def order_confirm(request, lead_id):
    """APC verified availability — capture the held deposit and book the order (APC-26).

    The only place in the app that turns an authorization into money, so it is gated on
    payment access like every other capture, and it is the moment the customer's booking
    actually becomes real: LA push, balance schedule and the service-date messages all
    follow from the `book_lead` this triggers.
    """
    lead, _ = _plan(lead_id)
    try:
        services.confirm_order(lead, user=request.user)
    except services.PaymentError as exc:
        return _json_error(str(exc))
    except stripe.error.StripeError as exc:
        # The hold can lapse on the issuer's clock between the page render and the click —
        # the queue deliberately still shows a Confirm button on a lapsed row. Say so
        # instead of 500ing into a generic "Request failed."
        return _json_error(
            getattr(exc, "user_message", None) or "Stripe could not capture this hold.",
            status=502,
        )
    messages.success(request, f"{lead.quote_no} confirmed — deposit captured.")
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def order_cancel(request, lead_id):
    """APC could not cover the trip — release the hold, no money ever moves (APC-26)."""
    lead, _ = _plan(lead_id)
    # Once captured, an order is cancelled through the refund path — this one only ever
    # releases a hold, and must not quietly mark a paid order lost with money still taken.
    if lead.status != Lead.Status.ENGAGED:
        return _json_error("Only an engaged order can have its authorization released.")
    try:
        services.cancel_order(
            lead, user=request.user, reason=(request.POST.get("reason") or "").strip()
        )
    except services.PaymentError as exc:
        return _json_error(str(exc))
    except stripe.error.StripeError as exc:
        return _json_error(
            getattr(exc, "user_message", None) or "Stripe could not release this hold.",
            status=502,
        )
    messages.success(request, f"{lead.quote_no} cancelled — the authorization was released.")
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def order_admin_intent(request, lead_id):
    lead, plan = _plan(lead_id)
    plan = plan or services.ensure_plan(lead)
    try:
        charge, client_secret = services.create_admin_payment_intent(
            plan, request.POST.get("amount")
        )
    except services.PaymentError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, "client_secret": client_secret, "charge_id": charge.pk})


@login_required
@payment_access_required
@require_POST
def order_admin_complete(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan is None:
        return _json_error("No payment plan on this quote.")
    pi_id = (request.POST.get("payment_intent_id") or "").strip()
    if not pi_id:
        return _json_error("Missing payment intent.")
    try:
        services.record_payment(plan, pi_id)
    except services.PaymentError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, "remaining": str(services.remaining_balance(lead))})


@login_required
@payment_access_required
@require_POST
def order_setup_intent(request, lead_id):
    lead, plan = _plan(lead_id)
    plan = plan or services.ensure_plan(lead)
    try:
        client_secret = services.create_setup_intent(plan)
    except services.PaymentError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True, "client_secret": client_secret})


@login_required
@payment_access_required
@require_POST
def order_save_card(request, lead_id):
    lead, plan = _plan(lead_id)
    plan = plan or services.ensure_plan(lead)
    pm_id = (request.POST.get("payment_method_id") or "").strip()
    if not pm_id:
        return _json_error("Missing payment method.")
    try:
        services.save_payment_method(plan, pm_id)
    except services.PaymentError as exc:
        return _json_error(str(exc))
    plan.refresh_from_db()
    return JsonResponse({"ok": True, "card_brand": plan.card_brand, "card_last4": plan.card_last4})


@login_required
@payment_access_required
@require_POST
def order_charge_saved(request, lead_id):
    lead, plan = _plan(lead_id)
    if plan is None:
        return _json_error("No payment plan on this quote.")
    try:
        services.charge_saved_card(plan, request.POST.get("amount"))
    except services.PaymentError as exc:
        return _json_error(str(exc))
    lead.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "remaining": str(services.remaining_balance(lead)),
            "status": lead.status,
        }
    )


_PAY_LINK_CHANNEL = {"sms": "phone", "email": "email"}
_PAY_LINK_MODEL = {"sms": Message.Channel.SMS, "email": Message.Channel.EMAIL}


@login_required
@payment_access_required
@require_POST
def order_send_pay_link(request, lead_id):
    """Send the customer pay-page link over Podium (SMS preferred, email fallback) and
    record it as an outbound message on the conversation."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=lead_id)
    contact = lead.contact

    if contact.phone:
        channel, identifier = "sms", contact.phone
    elif contact.email:
        channel, identifier = "email", contact.email
    else:
        return _json_error("No phone or email on file for this contact.")

    base_url = request.build_absolute_uri("/")[:-1]
    link = lead_services.make_pay_page_url(lead, base_url=base_url)
    body = f"Here's a secure link to pay for reservation {lead.quote_no}: {link}"

    try:
        response = podium.send_message(
            identifier=identifier, body=body, channel_type=_PAY_LINK_CHANNEL[channel]
        )
    except (PodiumAPIError, PodiumNotConnected) as exc:
        return _json_error(str(exc), status=502)

    uid = ""
    if isinstance(response, dict):
        uid = response.get("uid") or (response.get("data") or {}).get("uid") or ""

    messaging_services.record_outbound(
        messaging_services.conversation_for(contact),
        channel=_PAY_LINK_MODEL[channel],
        body=body,
        podium_message_uid=uid,
        sender_name=request.user.get_full_name() or request.user.username,
    )
    return JsonResponse({"ok": True, "channel": channel})
