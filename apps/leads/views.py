"""Leads & Quotes — list, filter, and the quote/reservations detail view."""

from __future__ import annotations

from decimal import Decimal

import stripe
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature
from django.core.validators import validate_email
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Cast, Concat
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import payment_access_required
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.integrations import la_sync
from apps.integrations.la_sync import IDEMPOTENCY_PREFIX
from apps.integrations.models import ZapEvent
from apps.messaging import touchpoints
from apps.messaging.models import TouchPoint
from apps.notifications.models import Notification
from apps.payments import ledger
from apps.payments import services as payment_services

from . import services
from .forms import NewLeadForm
from .models import Lead, VehicleType


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
        "stops": [
            {
                "address": s.address,
                "note": s.note,
                "name": s.name,
                "time": s.scheduled_time.strftime("%H:%M") if s.scheduled_time else "",
            }
            for s in r.stops.all()
        ],
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
        # quote_no is a computed property ("Q-{1040+pk}"), not a column — rebuild it
        # in SQL so the grid is searchable by "1065", "Q-1065", or a partial.
        leads = (
            leads.annotate(
                quote_ref=Concat(
                    Value("Q-"),
                    Cast(1040 + F("id"), output_field=CharField()),
                    output_field=CharField(),
                )
            )
            .filter(
                Q(quote_ref__icontains=query)
                | Q(contact__name__icontains=query)
                | Q(contact__company__name__icontains=query)
                | Q(contact__email__icontains=query)
                | Q(reservations__service__icontains=query)
            )
            .distinct()
        )

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
def pipeline(request: HttpRequest) -> HttpResponse:
    """Kanban of leads by status with per-column value (spec 2026-07-12 §1)."""
    leads = (
        Lead.objects.select_related("contact", "payment")
        .prefetch_related("reservations")
        .order_by("-created_at")
    )
    by_status: dict[str, list[Lead]] = {s: [] for s in Lead.Status.values}
    for lead in leads:
        by_status[lead.status].append(lead)
    columns = [
        {
            "status": status,
            "label": label,
            "leads": by_status[status],
            "value": sum((lead.quote_total for lead in by_status[status]), Decimal("0")),
        }
        for status, label in Lead.Status.choices
    ]
    return render(
        request,
        "leads/pipeline.html",
        {
            "nav": "pipeline",
            "page_title": "Pipeline",
            "columns": columns,
            "open_value": Lead.objects.open_pipeline_value(),
        },
    )


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
    _vehicles = list(VehicleType.objects.filter(active=True).values("id", "name"))
    reservations = lead.reservations.all()

    la_events: dict[int, ZapEvent] = {}
    events = ZapEvent.objects.filter(lead=lead, action=ZapEvent.Action.CREATE_RESERVATION)
    for event in events:
        key = event.idempotency_key.removeprefix(IDEMPOTENCY_PREFIX)
        if key.isdigit():
            la_events[int(key)] = event
    la_sync_rows = [
        {
            "reservation": res,
            "event": la_events.get(res.pk),
            "payload_dom_id": f"la-payload-{res.pk}",
        }
        for res in reservations
    ]
    la_configured = la_sync.limoanywhere.is_configured()
    can_resend_la = any(
        row["event"]
        and (
            row["event"].result == ZapEvent.Result.ERROR
            or (row["event"].result == ZapEvent.Result.PREVIEW and la_configured)
        )
        for row in la_sync_rows
    )

    context = {
        "nav": "leads",
        "page_title": lead.quote_no,
        "lead": lead,
        "reservations": reservations,
        "la_sync_rows": la_sync_rows,
        "can_resend_la": can_resend_la,
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

    normalized_phone = None  # tri-state: None = key absent, "" = explicit clear, str = new value
    if "phone" in request.POST:
        phone_val = request.POST.get("phone", "").strip()
        if phone_val:
            normalized_phone = to_e164(phone_val)
            if normalized_phone is None:
                return JsonResponse(
                    {"ok": False, "error": "Enter a valid phone number."}, status=400
                )
        else:
            normalized_phone = ""

    contact = lead.contact
    contact_fields = []
    for field in ("name", "email"):
        if field in request.POST:
            setattr(contact, field, request.POST.get(field, "").strip())
            contact_fields.append(field)
    if "company" in request.POST:
        from apps.contacts.models import Company

        contact.company = Company.objects.get_or_create_by_name(request.POST.get("company", ""))
        contact_fields.append("company")
    if normalized_phone is not None:
        contact.phone = normalized_phone
        contact_fields.append("phone")
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


def _wants_json(request: HttpRequest) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def _transition_refused(request: HttpRequest, message: str) -> HttpResponse:
    if _wants_json(request):
        return JsonResponse({"ok": False, "error": message}, status=400)
    return HttpResponse(message, status=400)


@login_required
@require_POST
def lead_mark_lost(request, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead, pk=pk)
    if not lead.can_transition(Lead.Status.LOST):
        message = (
            "Booked orders are cancelled from the Orders console."
            if lead.status == Lead.Status.BOOKED
            else "Already marked lost."
        )
        return _transition_refused(request, message)
    lead.status = Lead.Status.LOST
    lead.lost_reason = (request.POST.get("reason") or "").strip() or "Marked lost"
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    touchpoints.cancel_pending(lead, kinds=list(TouchPoint.Kind.values))
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": lead.status})
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_reopen(request, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead, pk=pk)
    if not lead.can_transition(Lead.Status.NEW):
        return _transition_refused(request, "Only lost leads can be reopened.")
    lead.status = Lead.Status.NEW
    lead.lost_reason = ""
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": lead.status})
    return redirect("lead_detail", pk=pk)


@login_required
@payment_access_required
@require_POST
def lead_resend_la(request, pk: int) -> HttpResponse:
    """Manually re-push every reservation on the lead to LimoAnywhere."""
    lead = get_object_or_404(Lead, pk=pk)
    la_sync.push_lead_bookings(lead)
    return redirect("lead_detail", pk=pk)


@login_required
@require_POST
def lead_send_quote(request, pk: int) -> JsonResponse:
    """Create/refresh the deposit plan, transition the lead, stamp the send/expiry, and
    deliver the public quote-page link on the requested channels. Returns the send result
    as JSON. ``channels`` (repeated POST field) selects "email"/"sms"; defaults to both."""
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)
    base_url = request.build_absolute_uri("/")[:-1]
    raw = request.POST.getlist("channels") or ["email", "sms"]
    channels = {c for c in raw if c in {"email", "sms"}} or {"email", "sms"}
    result = services.send_quote(lead, base_url=base_url, channels=channels)
    return JsonResponse(result.as_dict(), status=result.http_status)


def quote_deposit_success(request, token: str) -> HttpResponse:
    try:
        lead = services.read_deposit_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None
    plan = getattr(lead, "payment", None)
    return render(
        request,
        "public/deposit_success.html",
        {"quote_no": lead.quote_no, "deposit_amount": plan.deposit_amount if plan else None},
    )


def quote_deposit_cancel(request, token: str) -> HttpResponse:
    try:
        lead = services.read_deposit_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None
    return render(request, "public/deposit_cancel.html", {"quote_no": lead.quote_no})


def _quote_page_context(lead: Lead, token: str, *, error: str = "") -> dict:
    plan = getattr(lead, "payment", None)
    is_expired = lead.status == Lead.Status.QUOTED and lead.quote_expired
    is_active = lead.status == Lead.Status.QUOTED and not lead.quote_expired
    return {
        "lead": lead,
        "token": token,
        "quote_no": lead.quote_no,
        "reservations": lead.reservations.select_related("vehicle").prefetch_related("stops"),
        "billing_contact": lead.effective_billing_contact,
        "passenger_names": lead.passenger_names,
        "plan": plan,
        "deposit_pct": plan.deposit_pct if plan else None,
        "deposit_amount": plan.deposit_amount if plan else None,
        "quote_total": plan.quote_total if plan else lead.quote_total,
        "is_active": is_active,
        "is_expired": is_expired,
        "is_booked": lead.status == Lead.Status.BOOKED,
        "error": error,
    }


@require_GET
def quote_page(request, token: str) -> HttpResponse:
    """The customer-facing quote page a `send_quote` link points to. Forged/stale
    tokens 404; NEW/LOST leads (never quoted / dead) 404 too — only QUOTED and
    BOOKED are shown."""
    try:
        lead = services.read_deposit_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None
    if lead.status not in (Lead.Status.QUOTED, Lead.Status.BOOKED):
        raise Http404

    if lead.status == Lead.Status.QUOTED:
        if lead.quote_expired:
            already_notified = Notification.objects.filter(
                lead=lead, kind=Notification.Kind.QUOTE_EXPIRED
            ).exists()
            if not already_notified:
                Notification.notify(
                    lead,
                    Notification.Kind.QUOTE_EXPIRED,
                    title=f"Quote {lead.quote_no} expired",
                    detail="The customer opened an expired quote link.",
                )
        else:
            first_view = lead.quote_viewed_at is None
            lead.quote_view_count += 1
            lead.quote_last_viewed_at = timezone.now()
            fields = ["quote_view_count", "quote_last_viewed_at", "updated_at"]
            if first_view:
                lead.quote_viewed_at = lead.quote_last_viewed_at
                fields.append("quote_viewed_at")
            lead.save(update_fields=fields)
            if first_view:
                touchpoints.schedule_quote_viewed(lead)

    return render(request, "public/quote.html", _quote_page_context(lead, token))


@require_POST
def quote_book(request, token: str) -> HttpResponse:
    """Book-now: kick off the Stripe deposit Checkout for an active QUOTED lead."""
    try:
        lead = services.read_deposit_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None

    if lead.status != Lead.Status.QUOTED or lead.quote_expired:
        return redirect("quote_page", token=token)

    plan = getattr(lead, "payment", None)
    if plan is None:
        return redirect("quote_page", token=token)

    success_url = request.build_absolute_uri(reverse("quote_deposit_success", args=[token]))
    cancel_url = request.build_absolute_uri(reverse("quote_deposit_cancel", args=[token]))
    try:
        checkout_url = payment_services.create_deposit_checkout(
            plan, success_url=success_url, cancel_url=cancel_url
        )
    except stripe.error.StripeError as exc:
        message = getattr(exc, "user_message", None) or str(exc)
        return render(request, "public/quote.html", _quote_page_context(lead, token, error=message))

    return redirect(checkout_url)


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
        company_name=cd["company"],
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
    touchpoints.schedule_lead_created(lead)
    return redirect("lead_detail", pk=lead.pk)
