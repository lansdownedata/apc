from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import requests
import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.permissions import payment_access_required
from apps.billing import selectors as billing_selectors
from apps.core.choices import Channel
from apps.dispatch import selectors as dispatch_selectors
from apps.dispatch import services as dispatch_services
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads import contact_roles
from apps.leads import services as lead_services
from apps.leads.models import Lead
from apps.reservations import editor as reservation_editor
from apps.reservations import groups
from apps.reservations.models import Reservation, Stop
from apps.tasks import selectors as task_selectors

from . import ledger, payables, reports, services, webhooks
from .models import AffiliatePayable


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
def order_detail(request, lead_id):
    """One order: its trips, who is driving each of them, and its money.

    An order is a BOOKED lead — there is no Order model — so anything else redirects to
    the quote workspace, where that record actually lives, rather than 404ing on a lead
    that plainly exists.

    The trip lines and the money block are the same partials the workspace renders. What
    is different here is the emphasis: ops gets coverage and Edit, not the whole
    build-a-quote action set.
    """
    lead = get_object_or_404(
        Lead.objects.select_related(
            "contact", "payment", "billing_group__account"
        ).prefetch_related(
            "reservations__vehicle",
            "reservations__service_type",
            "reservations__extras",  # the editor's drafts list them; one query, not one per trip
            Prefetch(
                "reservations__stops",
                queryset=Stop.objects.select_related(
                    "airport", "airline", "flight", "flight__airport", "flight__airline"
                ).order_by("sequence"),
            ),
            dispatch_selectors.coverage_prefetch("reservations__assignments"),
        ),
        pk=lead_id,
    )
    if lead.status != Lead.Status.BOOKED:
        return redirect("lead_detail", pk=lead.pk)

    reservations = task_selectors.attach_green_lit(
        dispatch_selectors.attach_coverage(lead.reservations.all())
    )
    plan = getattr(lead, "payment", None)
    return render(
        request,
        "orders/order_detail.html",
        {
            "nav": "orders",
            "page_title": lead.quote_no,
            "lead": lead,
            # A linked set is several trips in the database and one line here (APC-14).
            "reservation_lines": groups.as_lines(reservations),
            "reservations": reservations,
            "order_tasks": task_selectors.checklist_for_order(lead),
            "is_booked": True,
            **lead_services.feedback_context(lead),
            **contact_roles.people_context(lead),
            # The same trip editor the quote workspace opens, fed identically.
            **reservation_editor.editor_context(request, lead, reservations),
            **reports.authorized_hold(lead),
            "payment": plan,
            "balances": reports.order_balances_with_remaining(lead),
            "stripe_pk": settings.STRIPE_PUBLISHABLE_KEY,
            "ledger_entries": lead.journal_entries.prefetch_related("lines").order_by(
                "posted_at", "id"
            ),
            "charges": list(plan.charges.all()) if plan else [],
            **billing_selectors.paid_by_context(lead),
        },
    )


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
        refundable = services.refundable_total(plan)
        try:
            amount = Decimal(request.POST.get("amount") or refundable)
            valid = amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            valid = False
        if not valid:
            messages.error(request, "Enter a valid refund amount.")
            return redirect("lead_detail", pk=lead_id)
        if amount > refundable:
            # Refused outright rather than silently capped: staff asked for more than the
            # card can take back, which is worth knowing before anything moves (APC-75).
            messages.error(request, f"You can refund at most ${refundable:,.2f} to the card.")
            return redirect("lead_detail", pk=lead_id)
        try:
            refunded = services.refund_payment(plan, amount)
        except stripe.error.StripeError as exc:
            messages.error(request, f"Stripe refused the refund: {exc.user_message or exc}")
            return redirect("lead_detail", pk=lead_id)
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
        # Everything still on the card, the post-trip final bill included (APC-75).
        try:
            services.refund_payment(plan, services.refundable_total(plan))
        except stripe.error.StripeError as exc:
            # Stop before cancelling: an order is never marked lost with money still on it
            # because Stripe said no. Whatever did refund is already on the books.
            messages.error(
                request,
                f"Stripe refused the refund, so the order was not cancelled: "
                f"{exc.user_message or exc}",
            )
            return redirect("lead_detail", pk=lead_id)
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
        try:
            services.charge_balance(plan)
        except services.PaymentError as exc:
            messages.error(request, str(exc))
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
    # Mirror of `order_cancel`'s guard. `record_payment` only calls `book_lead` from
    # NEW/QUOTED/ENGAGED, so capturing a hold on a lead someone has since marked LOST would
    # take the customer's money and never produce a booking, an LA push or a confirmation.
    if lead.status not in (Lead.Status.ENGAGED, Lead.Status.BOOKED):
        return _json_error("Only an engaged order can have its authorization captured.")
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


@login_required
@payment_access_required
@require_POST
def order_send_pay_link(request, lead_id):
    """Send the customer pay-page link over Podium (SMS preferred, email fallback) and
    record it as an outbound message on the conversation."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=lead_id)
    try:
        channel = services.send_pay_link(
            lead,
            base_url=request.build_absolute_uri("/")[:-1],
            sender_name=request.user.get_full_name() or request.user.username,
        )
    except services.PaymentError as exc:
        return _json_error(str(exc))
    except (PodiumAPIError, PodiumNotConnected) as exc:
        return _json_error(str(exc), status=502)
    except requests.RequestException:
        # A timeout or refused connection never reaches PodiumAPIError (that wraps HTTP
        # errors only); report it as the gateway failure it is, not a 500.
        return _json_error("Could not reach Podium. Please try again.", status=502)
    return JsonResponse({"ok": True, "channel": channel})


@login_required
@require_POST
def order_send_terms_link(request, lead_id):
    """APC-93 — send the customer the terms-acceptance link. Any staff member: it moves no
    money, and Customer Service owns the contract_signed task it closes."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=lead_id)
    try:
        channel = services.send_terms_link(
            lead,
            base_url=request.build_absolute_uri("/")[:-1],
            sender_name=request.user.get_full_name() or request.user.username,
        )
    except services.PaymentError as exc:
        return _json_error(str(exc))
    except (PodiumAPIError, PodiumNotConnected) as exc:
        return _json_error(str(exc), status=502)
    except requests.RequestException:
        # A timeout or refused connection never reaches PodiumAPIError (that wraps HTTP
        # errors only); report it as the gateway failure it is, not a 500.
        return _json_error("Could not reach Podium. Please try again.", status=502)
    return JsonResponse({"ok": True, "channel": channel})


# --- affiliate payables (APC-61): record only, nothing moves money -------------------


@login_required
@payment_access_required
@require_POST
def order_bill_to_account(request, lead_id):
    """APC-105: invoice the whole order to one of the customer's account groups."""
    from apps.billing import services as billing
    from apps.billing.models import AccountGroup

    lead = get_object_or_404(Lead, pk=lead_id)
    raw = request.POST.get("group", "")
    group = (
        AccountGroup.objects.select_related("account").filter(pk=raw).first()
        if raw.isdigit()
        else None
    )
    if group is None:
        return _json_error("Pick a billing group.")
    try:
        billing.bill_to_account(
            lead, group, po_number=request.POST.get("po_number", ""), user=request.user
        )
    except billing.BillingError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def order_bill_to_card(request, lead_id):
    """APC-105: back to card; the deposit and the balance schedule apply again."""
    from apps.billing import services as billing

    lead = get_object_or_404(Lead, pk=lead_id)
    try:
        billing.bill_to_card(lead, user=request.user)
    except billing.BillingError as exc:
        return _json_error(str(exc))
    return JsonResponse({"ok": True})


def _payable(pk: int) -> AffiliatePayable:
    return get_object_or_404(
        AffiliatePayable.objects.select_related("assignment__reservation", "assignment__vendor"),
        pk=pk,
    )


def _payable_ok(payable: AffiliatePayable) -> JsonResponse:
    payable.refresh_from_db()
    return JsonResponse({"ok": True, "payable": payables.payable_json(payable)})


@login_required
@payment_access_required
@require_POST
def payable_invoice(request, pk):
    payable = _payable(pk)
    raw = (request.POST.get("invoice_amount") or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(raw) if raw else None
    except InvalidOperation:
        return _json_error("The invoice amount has to be an amount.")
    if amount is not None and (not amount.is_finite() or amount >= Decimal("99999999.995")):
        return _json_error("The invoice amount has to be an amount.")
    if amount is not None:
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    try:
        payables.record_invoice(
            payable,
            user=request.user,
            number=request.POST.get("invoice_number", ""),
            amount=amount,
            file=request.FILES.get("invoice_file"),
        )
    except payables.PayableError as exc:
        return _json_error(str(exc))
    return _payable_ok(payable)


@login_required
@payment_access_required
@require_POST
def payable_approve(request, pk):
    payable = _payable(pk)
    try:
        payables.approve(payable, user=request.user, note=request.POST.get("note", ""))
    except payables.PayableError as exc:
        return _json_error(str(exc))
    return _payable_ok(payable)


@login_required
@payment_access_required
@require_POST
def payable_paid(request, pk):
    payable = _payable(pk)
    try:
        payables.mark_paid(
            payable,
            user=request.user,
            method=request.POST.get("method", ""),
            reference=request.POST.get("reference", ""),
        )
    except payables.PayableError as exc:
        return _json_error(str(exc))
    return _payable_ok(payable)


@login_required
@payment_access_required
@require_POST
def payable_sync(request, pk):
    """Retry QuickBooks for one payable, now: its vendor if needed, its bill, its payment
    (APC-74). A person asked, so this one call may wait on Intuit."""
    from apps.integrations import qbo

    from . import qbo_bills

    payable = _payable(pk)
    if not qbo.is_connected():
        return _json_error("QuickBooks isn't connected.")
    try:
        reason = qbo_bills.push_everything_for(payable)
    except qbo.QboNotConnected as exc:
        return _json_error(str(exc))
    except qbo_bills.SyncPaused:
        return _json_error("QuickBooks couldn't be reached. Try again in a minute.", status=502)
    payable.refresh_from_db()
    return JsonResponse({"ok": True, "payable": payables.payable_json(payable), "reason": reason})


@login_required
@payment_access_required
def order_receipt(request, pk):
    """Staff view of a customer's receipt (APC-103): the same email the customer got, and
    whether it went."""
    from django.http import Http404
    from django.template.loader import render_to_string

    from . import receipts
    from .models import Charge

    charge = get_object_or_404(Charge.objects.select_related("reservation__lead__contact"), pk=pk)
    if not receipts.is_receipt(charge):
        raise Http404("Not a receipt")
    context = receipts.receipt_context(charge)
    return render(
        request,
        "payments/receipt.html",
        {
            "nav": "processing",
            "page_title": f"Receipt {charge.receipt_number}",
            "charge": charge,
            "trip": charge.reservation,
            "recipient": receipts.recipient(charge),
            "subject": receipts.subject(context),
            "email_html": render_to_string("email/trip_receipt.html", context),
        },
    )
