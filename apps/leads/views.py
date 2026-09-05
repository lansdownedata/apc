"""Leads & Quotes — list, filter, and the quote/reservations detail view."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature
from django.core.validators import validate_email
from django.db import IntegrityError
from django.db.models import CharField, F, Prefetch, Q, Value
from django.db.models.functions import Cast, Concat
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import payment_access_required
from apps.contacts import services as contact_services
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
from apps.payments import reports as payment_reports
from apps.payments import services as payment_services
from apps.reservations import groups
from apps.reservations import services as reservation_services
from apps.reservations.models import Stop

from . import services
from .forms import NewLeadForm, PortalWeddingForm
from .models import QUOTE_NUMBER_BASE, QUOTE_PREFIX, Lead, VehicleType

ZERO = Decimal("0.00")


def _balances_with_remaining(lead: Lead) -> dict:
    bals = ledger.order_balances(lead)
    bals["remaining"] = payment_services.remaining_balance(lead)
    return bals


def _la_state(rows: list[dict]) -> str:
    """One word for the header chip — the worst thing that happened to any trip.

    "sent" needs every trip through; a booking that was never attempted must not hide
    behind the ones that were.
    """
    results = {row["event"].result if row["event"] else "" for row in rows}
    if ZapEvent.Result.ERROR in results:
        return "error"
    if ZapEvent.Result.PREVIEW in results:
        return "preview"
    if rows and results == {ZapEvent.Result.SUCCESS}:
        return "sent"
    return "unsent"


def _open_editor_id(request, reservations) -> int | None:
    """The `?edit=<pk>` trip to reopen the editor on, if it names a real trip on this
    quote — a redirect from Create Return Trip (APC-15) uses it. Anything else is None."""
    raw = request.GET.get("edit", "")
    if not raw.isdigit():
        return None
    pk = int(raw)
    return pk if any(r.pk == pk for r in reservations) else None


def _reservation_draft(r, *, quantity: int = 1) -> dict:
    """The editor's view of one saved trip. `quantity` is the size of the linked set it
    belongs to (APC-14) — 1 for a trip that stands alone."""
    return {
        "id": r.pk,
        "quantity": quantity,
        "tripType": r.trip_type,
        "serviceType": r.service_type_id or "",
        "date": r.pickup_date.isoformat() if r.pickup_date else "",
        "time": r.pickup_time.strftime("%H:%M") if r.pickup_time else "",
        "vehicle": r.vehicle_id or "",
        "pax": r.passengers,
        "rate": float(r.rate),
        "hours": float(r.hours),
        "minHours": float(r.min_hours),
        "gratuityPct": float(r.gratuity_pct),
        "gratuityFlat": float(r.gratuity_flat),
        "discountPct": float(r.discount_pct),
        "discountFlat": float(r.discount_flat),
        "dropoffDate": r.dropoff_date.isoformat() if r.dropoff_date else "",
        "dropoffTime": r.dropoff_time.strftime("%H:%M") if r.dropoff_time else "",
        "stops": [
            {
                "address": s.address,
                "note": s.note,
                "name": s.name,
                "time": s.scheduled_time.strftime("%H:%M") if s.scheduled_time else "",
                # Round-tripped so the delete-and-recreate in save_reservation_from_draft
                # doesn't drop coordinates the user already picked.
                "lat": str(s.latitude) if s.latitude is not None else "",
                "lng": str(s.longitude) if s.longitude is not None else "",
                "airport": s.airport_id or "",
                "airportCode": s.airport.iata if s.airport_id else "",
                # Gates the editor's Verify button (spec 2026-08-29 finding 2) — a stop's
                # airport can have a real IATA code and still have no scheduled service
                # (Andrews, Manassas, ...).
                "hasScheduledService": bool(s.airport_id and s.airport.has_scheduled_service),
                "airline": s.airline_id or "",
                "flight": s.flight_number,
                "direction": s.flight_direction,
                # Pre-rendered pill for a stop already linked to a cached flight, so the
                # editor opens with the check shown. Client-only; the parser ignores it.
                "pill": s.flight_pill,
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
        # quote_no is a computed property (Lead.QUOTE_PREFIX + QUOTE_NUMBER_BASE + pk),
        # not a column — rebuild it in SQL so the grid is searchable by "100065",
        # "APC-100065", or a partial. Must mirror Lead.quote_no exactly.
        leads = (
            leads.annotate(
                quote_ref=Concat(
                    Value(f"{QUOTE_PREFIX}-"),
                    Cast(QUOTE_NUMBER_BASE + F("id"), output_field=CharField()),
                    output_field=CharField(),
                )
            )
            .filter(
                Q(quote_ref__icontains=query)
                | Q(contact__name__icontains=query)
                | Q(contact__company__name__icontains=query)
                | Q(contact__email__icontains=query)
                | Q(reservations__service_type__name__icontains=query)
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
        "agent_options": services.agent_options(),
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


def _wedding_state(lead) -> dict | None:
    """The saved wedding plan, ready for `weddingPlanner()`, or None when this is not one.

    Each leg is seeded with the vehicle already assigned to its reservation, so reopening
    the builder shows what the agent chose rather than recommending all over again.
    """
    payload = lead.intake_payload or {}
    if not payload.get("legs"):
        return None
    saved = {
        r.source_leg_id: {
            "vehicle_id": r.vehicle_id,
            "trip_type": r.trip_type,
            # 0 is "no override, bill the rate-card minimum" — send null, not 0, so the
            # Hours box shows its "min" placeholder rather than a literal zero.
            "hours": float(r.hours) if r.hours else None,
        }
        for r in lead.reservations.exclude(source_leg_id="")
    }
    legs = [{**leg, **saved.get(leg.get("id"), {})} for leg in payload["legs"]]
    return {**payload, "legs": legs, "portal": True}


@login_required
def lead_detail(request, pk):
    lead = get_object_or_404(
        Lead.objects.select_related("contact", "assigned_agent").prefetch_related(
            "reservations__vehicle",
            "reservations__service_type",
            Prefetch(
                "reservations__stops",
                queryset=Stop.objects.select_related(
                    "airport", "airline", "flight", "flight__airport", "flight__airline"
                ).order_by("sequence"),
            ),
            "notifications",
        ),
        pk=pk,
    )
    _vehicles = list(
        VehicleType.objects.filter(active=True).values(
            "id", "name", "rate", "hourly_min_hours", "transfer_min_hours"
        )
    )
    reservations = lead.reservations.all()
    # A linked set is several trips in the database and one line on the screen (APC-14).
    reservation_lines = groups.as_lines(reservations)
    group_sizes = {m.pk: line.size for line in reservation_lines for m in line.members}

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
        "booking_intent": request.GET.get("booking") == "1",
        "wedding_state": _wedding_state(lead),
        "is_wedding": any(reservation_services.is_wedding_trip(r) for r in reservations),
        # The held deposit + its deadline, for the Confirm/Cancel controls (APC-26).
        **payment_reports.authorized_hold(lead),
        "wedding_open": request.GET.get("wedding") == "1",
        # ?edit=<pk> reopens the editor on one trip after a redirect (APC-15 return trip).
        "open_editor_id": _open_editor_id(request, reservations),
        "reservations": reservations,
        "la_sync_rows": la_sync_rows,
        "la_state": _la_state(la_sync_rows),
        "can_resend_la": can_resend_la,
        "payment": getattr(lead, "payment", None),
        "balances": _balances_with_remaining(lead),
        "stripe_pk": settings.STRIPE_PUBLISHABLE_KEY,
        "ledger_entries": lead.journal_entries.prefetch_related("lines").order_by(
            "posted_at", "id"
        ),
        "charges": [c for p in [getattr(lead, "payment", None)] if p for c in p.charges.all()],
        "channels": Channel.choices,
        "agents": services.agent_options(),
        "reservation_lines": reservation_lines,
        "duplicate_max": groups.DUPLICATE_MAX,
        "reservations_json": [
            _reservation_draft(r, quantity=group_sizes.get(r.pk, 1)) for r in reservations
        ],
        "vehicles_json": [
            {
                "id": v["id"],
                "name": v["name"],
                "rate": float(v["rate"]),
                "hourlyMin": float(v["hourly_min_hours"]),
                "transferMin": float(v["transfer_min_hours"]),
            }
            for v in _vehicles
        ],
        "vehicle_options": [(v["id"], v["name"]) for v in _vehicles],
        "service_type_options": services.service_type_options(lead),
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

    # Day-of wedding details (APC-18) — usually filled by the couple on the T-7d link's
    # own form, but staff can key in what a couple calls in with the same field.
    normalized_day_of_phone = None  # tri-state, same convention as the contact phone above
    if "day_of_contact_phone" in request.POST:
        phone_val = request.POST.get("day_of_contact_phone", "").strip()
        if phone_val:
            normalized_day_of_phone = to_e164(phone_val)
            if normalized_day_of_phone is None:
                return JsonResponse(
                    {"ok": False, "error": "Enter a valid day-of contact phone number."},
                    status=400,
                )
        else:
            normalized_day_of_phone = ""

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
        try:
            contact.save(update_fields=contact_fields + ["updated_at"])
        except IntegrityError:
            return JsonResponse(
                {"ok": False, "error": "That email is already used by another contact."},
                status=400,
            )

    lead_fields = []
    channel = request.POST.get("channel")
    if channel in Channel.values:
        lead.channel = channel
        lead_fields.append("channel")
    if "agent" in request.POST:
        agent_id = request.POST.get("agent") or None
        lead.assigned_agent_id = int(agent_id) if agent_id else None
        lead_fields.append("assigned_agent")
    if "wedding_name" in request.POST:
        lead.wedding_name = request.POST.get("wedding_name", "").strip()[:200]
        lead_fields.append("wedding_name")
    if "day_of_contact_name" in request.POST:
        lead.day_of_contact_name = request.POST.get("day_of_contact_name", "").strip()[:200]
        lead_fields.append("day_of_contact_name")
    if normalized_day_of_phone is not None:
        lead.day_of_contact_phone = normalized_day_of_phone
        lead_fields.append("day_of_contact_phone")
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
@require_POST
def lead_mark_booked(request, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead, pk=pk)
    if lead.status == Lead.Status.BOOKED:
        if _wants_json(request):
            return JsonResponse({"ok": True, "status": lead.status})
        return redirect("lead_detail", pk=pk)
    if not lead.can_transition(Lead.Status.BOOKED):
        return _transition_refused(request, "Only new or quoted leads can be booked.")
    if not lead.reservations.exists():
        return _transition_refused(request, "Add at least one trip before booking.")
    try:
        services.book_lead(lead)
    except services.BookLeadError as exc:
        return _transition_refused(request, str(exc))
    if _wants_json(request):
        return JsonResponse({"ok": True, "status": lead.status})
    messages.success(request, "Order booked.")
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
def lead_reissue_quote(request, pk: int) -> HttpResponse:
    """Reissue an expired quote (APC-25) — fresh expiry + restarted touch-points, no re-send."""
    lead = get_object_or_404(Lead, pk=pk)
    try:
        expiry = services.reissue_quote(lead)
    except services.ReissueQuoteError as exc:
        messages.error(request, str(exc))
        return redirect("lead_detail", pk=pk)
    messages.success(request, f"Quote reissued — now expires {expiry:%b %-d, %Y}.")
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
    """The landing page after a successful confirm. Also the `return_url` for a 3-D Secure
    challenge — Stripe appends `?payment_intent=…&redirect_status=succeeded`, and the
    `complete/` POST never ran, so reconcile it here. Idempotent against the webhook."""
    lead = _lead_from_token(token)
    plan = getattr(lead, "payment", None)

    pi_id = request.GET.get("payment_intent")
    if plan is not None and pi_id and request.GET.get("redirect_status") == "succeeded":
        charge = plan.charges.filter(stripe_payment_intent_id=pi_id).first()
        if charge is not None and charge.status != charge.Status.SUCCEEDED:
            try:
                payment_services.record_payment(plan, pi_id, kind=charge.kind)
            except payment_services.PaymentError:
                pass

    paid_in_full = plan is not None and payment_services.remaining_balance(lead) <= ZERO
    return render(
        request,
        "public/deposit_success.html",
        {
            "quote_no": lead.quote_no,
            "deposit_amount": plan.deposit_amount if plan else None,
            "paid_in_full": paid_in_full,
        },
    )


def _quote_page_context(lead: Lead, token: str, *, error: str = "") -> dict:
    plan = getattr(lead, "payment", None)
    deposit_unpaid = plan is None or plan.deposit_status != plan.DepositStatus.PAID
    is_expired = lead.status == Lead.Status.QUOTED and lead.quote_expired
    is_active = (lead.status == Lead.Status.QUOTED and not lead.quote_expired) or (
        lead.status == Lead.Status.BOOKED and deposit_unpaid
    )
    return {
        "lead": lead,
        "token": token,
        "quote_no": lead.quote_no,
        "reservations": lead.reservations.select_related("vehicle").prefetch_related(
            Prefetch("stops", queryset=Stop.objects.select_related("airline").order_by("sequence"))
        ),
        "billing_contact": lead.effective_billing_contact,
        "passenger_names": lead.passenger_names,
        "plan": plan,
        "deposit_pct": plan.deposit_pct if plan else None,
        "deposit_amount": plan.deposit_amount if plan else None,
        "quote_total": plan.quote_total if plan else lead.quote_total,
        "is_active": is_active,
        "is_expired": is_expired,
        "is_booked": lead.status == Lead.Status.BOOKED and not deposit_unpaid,
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


def _lead_from_token(token: str) -> Lead:
    try:
        return services.read_deposit_token(token)
    except (BadSignature, Lead.DoesNotExist):
        raise Http404 from None


def _owed(lead: Lead) -> tuple[str | None, Decimal]:
    """(Charge.Kind, amount) for what the customer still owes, or (None, 0).

    Resolved from the ledger, never the plan flags alone — a partial staff charge must be
    reflected, and `deposit_status` can lag it. The deposit branch is clamped by the total
    still owed so a deposit percentage larger than the remainder can never over-charge.
    """
    from apps.payments.models import Charge

    plan = getattr(lead, "payment", None)
    if plan is None:
        return None, ZERO
    owed = payment_services.remaining_balance(lead)
    if owed <= ZERO:
        return None, ZERO
    collected = ledger.order_balances(lead)["collected"]
    if collected < plan.deposit_amount:
        return Charge.Kind.DEPOSIT, min(plan.deposit_amount - collected, owed)
    return Charge.Kind.BALANCE, owed


def _pay_state(lead: Lead) -> tuple[str | None, Decimal]:
    """`_owed`, but gated on the states a pay form must not appear in."""
    if lead.status == Lead.Status.LOST:
        return None, ZERO
    if lead.status == Lead.Status.QUOTED and lead.quote_expired:
        return None, ZERO
    if lead.status == Lead.Status.ENGAGED:
        # Their deposit is authorized and held (APC-26). `_owed` reads the ledger, which an
        # authorization deliberately never touches, so without this the pay page would ask
        # a customer who has already paid to place a second hold.
        return None, ZERO
    return _owed(lead)


@require_GET
def quote_pay(request, token: str) -> HttpResponse:
    """The customer pay page — deposit or balance, whichever is owed."""
    lead = _lead_from_token(token)
    kind, amount = _pay_state(lead)
    return render(
        request,
        "public/pay.html",
        {
            "token": token,
            "quote_no": lead.quote_no,
            "lead": lead,
            "pay_kind": kind,
            "amount": amount,
            "amount_cents": int(amount * 100),
            "is_expired": lead.status == Lead.Status.QUOTED and lead.quote_expired,
            "is_lost": lead.status == Lead.Status.LOST,
            "stripe_pk": settings.STRIPE_PUBLISHABLE_KEY,
            "success_url": request.build_absolute_uri(
                reverse("quote_deposit_success", args=[token])
            ),
        },
    )


@require_POST
def quote_pay_intent(request, token: str) -> JsonResponse:
    """Mint (or reuse) the PaymentIntent for whatever is owed. Token-keyed; CSRF still on."""
    lead = _lead_from_token(token)
    kind, amount = _pay_state(lead)
    if kind is None:
        return JsonResponse(
            {"ok": False, "error": "There is nothing to pay on this quote."}, status=400
        )
    plan = payment_services.ensure_plan(lead)
    _, secret = payment_services.open_intent_for(plan, kind=kind, amount=amount)
    return JsonResponse({"ok": True, "client_secret": secret, "amount": str(amount)})


@require_POST
def quote_pay_complete(request, token: str) -> JsonResponse:
    """Reconcile immediately after the customer confirms (the no-redirect path)."""
    lead = _lead_from_token(token)
    plan = getattr(lead, "payment", None)
    pi_id = (request.POST.get("payment_intent_id") or "").strip()
    if plan is None or not pi_id:
        return JsonResponse({"ok": False, "error": "Missing payment."}, status=400)
    charge = plan.charges.filter(stripe_payment_intent_id=pi_id).first()
    if charge is None:
        # The token identifies the lead; an intent from another lead must not reconcile here.
        return JsonResponse({"ok": False, "error": "Unknown payment."}, status=400)
    from apps.payments.models import Charge

    try:
        # A deposit only authorizes at checkout (APC-26) — the intent is `requires_capture`,
        # not `succeeded`, so reconciling it as a payment would fail. The webhook is the
        # safety net; this is what makes the state flip feel instant.
        if charge.kind == Charge.Kind.DEPOSIT:
            payment_services.record_authorization(plan, pi_id)
        else:
            payment_services.record_payment(plan, pi_id, kind=charge.kind)
    except payment_services.PaymentError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


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
    picked = cd.get("contact_id")
    if picked is not None:
        # The agent explicitly said "this is that customer", so their edits are an
        # intentional profile update. `channel` stays out of it — see apply_booking_edits.
        contact = picked
        warning = contact_services.apply_booking_edits(
            contact,
            name=cd["name"],
            company=cd["company"],
            phone=cd["phone"],
            email=cd["email"],
        )
        if warning:
            messages.warning(request, warning)
    else:
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
    intent = cd.get("intent")
    if intent == "booking":
        return redirect(f"{reverse('lead_detail', args=[lead.pk])}?booking=1")
    if intent == "wedding":
        return redirect(f"{reverse('lead_detail', args=[lead.pk])}?wedding=1")
    touchpoints.schedule_lead_created(lead)
    return redirect("lead_detail", pk=lead.pk)


@login_required
@require_POST
def lead_wedding_save(request, pk: int) -> HttpResponse:
    """Rebuild a lead's wedding trips from the builder's plan (spec 2026-08-30 §6.1).

    No honeypot and no throttle, unlike the public POST — this one is behind auth.
    """
    lead = get_object_or_404(Lead.objects.select_related("contact"), pk=pk)
    form = PortalWeddingForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "; ".join(f"{k}: {e[0]}" for k, e in form.errors.items()) or "Could not save the day.",
        )
        return redirect("lead_detail", pk=lead.pk)
    result = services.rebuild_wedding_trips(lead, form.cleaned_data)
    if result.orphans:
        # Never deleted for the agent — a trip may already be priced, pushed to
        # LimoAnywhere or assigned to an affiliate.
        names = ", ".join(
            f"{r.pickup_time:%-I:%M %p} {stop.name}" if (stop := r.stops.first()) else "a trip"
            for r in result.orphans
        )
        messages.warning(
            request,
            f"No longer in the plan: {names}. They're still on the quote — remove them "
            "from the trip list if they're off.",
        )
    return redirect("lead_detail", pk=lead.pk)
