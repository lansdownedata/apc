"""Settings screens — owner-admin only."""

from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.permissions import owner_admin_required
from apps.addresses.models import Venue
from apps.dispatch.models import DispatchAlertConfig, DispatchException
from apps.fleet.forms import RenewalTypeForm
from apps.fleet.models import RENEWAL_PREFETCH, Driver, RenewalType, Vehicle
from apps.leads.models import Lead, ServiceType, VehicleType
from apps.messaging import fields, overrides
from apps.messaging.models import NotificationConfig
from apps.reservations.models import PricingConfig
from apps.tasks import schedule
from apps.tasks.models import TaskConfig
from apps.vendors.models import AffiliateDocumentType

from .forms import (
    AffiliateDocumentTypeForm,
    ChecklistScheduleForm,
    DispatchAlertConfigForm,
    MessageTemplateForm,
    PricingConfigForm,
    ServiceTypeForm,
    TaskConfigForm,
    VehicleTypeForm,
    VenueForm,
)


def _fleet_attention_count() -> int:
    """Active drivers + units whose paperwork is lapsed or expiring — the tile's nudge."""
    subjects = [
        *Driver.objects.filter(status=Driver.Status.ACTIVE).prefetch_related(RENEWAL_PREFETCH),
        *Vehicle.objects.filter(status=Vehicle.Status.ACTIVE).prefetch_related(RENEWAL_PREFETCH),
    ]
    return sum(1 for s in subjects if s.needs_attention)


_SHARED_SWITCH = frozenset({"thank_you_wedding", "trip_confirm_customer_2"})


@login_required
@owner_admin_required
def settings_index(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/index.html",
        {
            "nav": "settings",
            "page_title": "Settings",
            "vehicle_type_count": VehicleType.objects.filter(active=True).count(),
            "service_type_count": ServiceType.objects.filter(active=True).count(),
            "poi_count": Venue.objects.filter(is_active=True).count(),
            # Places that cannot take our largest coach — the number worth glancing at,
            # because each one reshapes any run quoted to it.
            "capped_poi_count": Venue.objects.filter(is_active=True)
            .exclude(max_vehicle__isnull=True, vehicle_cap__isnull=True)
            .count(),
            "renewal_type_count": RenewalType.objects.filter(active=True).count(),
            "fleet_attention_count": _fleet_attention_count(),
            "dispatch_monitor_on": DispatchAlertConfig.load().enabled,
            "open_dispatch_exceptions": DispatchException.objects.filter(
                resolved_at__isnull=True
            ).count(),
            "notifications_on": NotificationConfig.load().enabled,
            "tasks_on": TaskConfig.load().enabled,
            "default_cost_ratio_pct": PricingConfig.load().default_cost_ratio_pct,
            **_qbo_tile(),
        },
    )


def _qbo_tile() -> dict:
    from apps.integrations import qbo

    return {
        "qbo_enabled": qbo.enabled(),
        "qbo_tile_connected": qbo.enabled() and qbo.is_connected(),
    }


@login_required
@owner_admin_required
def vehicle_type_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/vehicle_type_list.html",
        {
            "nav": "settings",
            "page_title": "Vehicle Types",
            "vehicle_types": VehicleType.objects.all(),
        },
    )


@login_required
@owner_admin_required
def vehicle_type_create(request: HttpRequest) -> HttpResponse:
    form = VehicleTypeForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Vehicle type added.")
        return redirect("vehicle_type_list")
    return render(
        request,
        "settings/vehicle_type_form.html",
        {"nav": "settings", "page_title": "New vehicle type", "form": form, "target": None},
    )


@login_required
@owner_admin_required
def vehicle_type_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(VehicleType, pk=pk)
    form = VehicleTypeForm(request.POST or None, request.FILES or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Vehicle type updated.")
        return redirect("vehicle_type_list")
    return render(
        request,
        "settings/vehicle_type_form.html",
        {"nav": "settings", "page_title": target.name, "form": form, "target": target},
    )


@login_required
@owner_admin_required
@require_POST
def vehicle_type_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Deactivate rather than delete when reservations (SET_NULL — a hard delete would blank
    the vehicle on historical quotes) or fleet units (PROTECT — a hard delete would 500)
    reference this type."""
    target = get_object_or_404(VehicleType, pk=pk)
    if target.reservation_set.exists() or target.units.exists():
        target.active = False
        target.save(update_fields=["active", "updated_at"])
        messages.success(request, f"{target.name} deactivated (it's used by existing trips).")
    else:
        name = target.name
        target.delete()
        messages.success(request, f"{name} deleted.")
    return redirect("vehicle_type_list")


@login_required
@owner_admin_required
def service_type_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/service_type_list.html",
        {
            "nav": "settings",
            "page_title": "Service types",
            "service_types": ServiceType.objects.all(),
        },
    )


@login_required
@owner_admin_required
def service_type_create(request: HttpRequest) -> HttpResponse:
    form = ServiceTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Service type added.")
        return redirect("service_type_list")
    return render(
        request,
        "settings/service_type_form.html",
        {"nav": "settings", "page_title": "New service type", "form": form, "target": None},
    )


@login_required
@owner_admin_required
def service_type_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(ServiceType, pk=pk)
    form = ServiceTypeForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Service type updated.")
        return redirect("service_type_list")
    return render(
        request,
        "settings/service_type_form.html",
        {"nav": "settings", "page_title": target.name, "form": form, "target": target},
    )


@login_required
@owner_admin_required
@require_POST
def service_type_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Deactivate when trips reference this type — SET_NULL means a hard delete would
    silently blank the service on historical quotes."""
    target = get_object_or_404(ServiceType, pk=pk)
    if target.reservation_set.exists():
        target.active = False
        target.save(update_fields=["active", "updated_at"])
        messages.success(request, f"{target.name} deactivated (it's used by existing trips).")
    else:
        name = target.name
        target.delete()
        messages.success(request, f"{name} deleted.")
    return redirect("service_type_list")


@login_required
@owner_admin_required
def renewal_type_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/renewal_type_list.html",
        {
            "nav": "settings",
            "page_title": "Renewal types",
            "renewal_types": RenewalType.objects.all(),
        },
    )


@login_required
@owner_admin_required
def renewal_type_create(request: HttpRequest) -> HttpResponse:
    form = RenewalTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Renewal type added.")
        return redirect("renewal_type_list")
    return render(
        request,
        "settings/renewal_type_form.html",
        {"nav": "settings", "page_title": "New renewal type", "form": form, "target": None},
    )


@login_required
@owner_admin_required
def renewal_type_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(RenewalType, pk=pk)
    form = RenewalTypeForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Renewal type updated.")
        return redirect("renewal_type_list")
    return render(
        request,
        "settings/renewal_type_form.html",
        {"nav": "settings", "page_title": target.name, "form": form, "target": target},
    )


@login_required
@owner_admin_required
@require_POST
def renewal_type_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Deactivate when renewals reference the type (PROTECT) — a lapsed inspection doesn't
    stop mattering because the type was retired."""
    target = get_object_or_404(RenewalType, pk=pk)
    if target.renewals.exists():
        target.active = False
        target.save(update_fields=["active", "updated_at"])
        messages.success(request, f"{target.name} deactivated (it has renewal records).")
    else:
        name = target.name
        target.delete()
        messages.success(request, f"{name} deleted.")
    return redirect("renewal_type_list")


def _switch_label(kind: str) -> str:
    """The label of the message whose switch `kind` follows, or "" when it has its own."""
    owner = overrides.switch_owner(kind)
    return next(m.label for m in overrides.MESSAGES if m.kind == owner) if owner else ""


def _channels_text(rows: dict, kind: str) -> str:
    names = {"email": "Email", "sms": "SMS"}
    channels = overrides.effective(kind, rows).channels
    return " & ".join(names[c] for c in ("email", "sms") if c in channels)


@login_required
@owner_admin_required
def messages_list(request: HttpRequest) -> HttpResponse:
    """Every automated message, grouped, with its timing and its own on/off switch."""
    rows = overrides.load()
    master = NotificationConfig.load().enabled
    groups: list[dict] = []
    for spec in overrides.MESSAGES:
        if not groups or groups[-1]["name"] != spec.group:
            groups.append({"name": spec.group, "items": []})
        groups[-1]["items"].append(
            {
                "kind": spec.kind,
                "label": spec.label,
                "timing": overrides.timing_phrase(spec.kind, rows),
                "edited": spec.kind in rows,
                "enabled": overrides.is_enabled(spec.kind, rows, master=True),
                "shared_switch": _switch_label(spec.kind),
                "channels": _channels_text(rows, spec.kind),
            }
        )
    return render(
        request,
        "settings/messages.html",
        {"nav": "settings", "page_title": "Messages", "groups": groups, "master_on": master},
    )


@login_required
@owner_admin_required
@require_POST
def message_toggle(request: HttpRequest, kind: str) -> JsonResponse:
    """Flip one message's switch, leaving its text and timing as they are."""
    if kind not in {m.kind for m in overrides.MESSAGES} or kind in _SHARED_SWITCH:
        raise Http404
    rows = overrides.load()
    enabled = not overrides.is_enabled(kind, rows, master=True)
    current = overrides.effective(kind, rows)
    try:
        overrides.save(
            kind,
            subject=current.subject,
            email_body=current.email_body,
            sms_body=current.sms_body,
            timing=overrides.timing_of(kind, rows),
            enabled=enabled,
            user=request.user,
        )
    except overrides.SaveError as exc:
        return JsonResponse({"ok": False, "error": next(iter(exc.errors.values()))}, status=400)
    return JsonResponse({"ok": True, "enabled": enabled})


@login_required
@owner_admin_required
@require_POST
def messages_master(request: HttpRequest) -> JsonResponse:
    """Flip the master switch that stops every automated message."""
    config = NotificationConfig.load()
    config.enabled = not config.enabled
    config.save(update_fields=["enabled"])
    return JsonResponse({"ok": True, "enabled": config.enabled})


def _message_spec(kind: str):
    spec = next((m for m in overrides.MESSAGES if m.kind == kind), None)
    if spec is None:
        raise Http404
    return spec


def _order_label(lead) -> str:
    pickups = [r.pickup_date for r in lead.reservations.all() if r.pickup_date]
    first = f"{min(pickups):%b %-d, %Y}" if pickups else "no date"
    return f"{lead.quote_no} · {lead.contact.name} · {first}"


@login_required
@owner_admin_required
def message_edit(request: HttpRequest, kind: str) -> HttpResponse:
    """One message: wording per channel, timing and switch, with field chips and a live preview."""
    spec = _message_spec(kind)
    form = MessageTemplateForm(kind, request.POST or None, has_switch=kind not in _SHARED_SWITCH)
    if request.method == "POST" and form.is_valid():
        count = form.save(request.user)
        note = f" {count} scheduled message{'s' if count != 1 else ''} re-dated." if count else ""
        messages.success(request, f"Saved. Applies to messages not yet sent.{note}")
        return redirect("messages")
    orders = overrides.preview_orders(kind)
    box_errors = {name: form.errors[name][0] for name in form.TEXTS if name in form.errors}
    first_error = next((n for n in form.TEXTS if n in box_errors), None)
    tab = "sms" if first_error == "sms_body" else "email" if first_error else ""
    channels = [{"key": c, "label": "Email" if c == "email" else "SMS"} for c in form.channels]
    chips = [{"key": f.key, "label": f.label, "group": f.group} for f in fields.fields_for(kind)]
    return render(
        request,
        "settings/message_edit.html",
        {
            "nav": "settings",
            "page_title": spec.label,
            "spec": spec,
            "form": form,
            "channels": channels,
            "tab": tab or channels[0]["key"],
            "box_errors": box_errors,
            "chips": chips,
            "orders": [(lead.pk, _order_label(lead)) for lead in orders],
            "default_order": orders[0].pk if orders else "",
            "anchor_phrase": overrides.timing_anchor_phrase(kind) if form.has_timing else "",
            "edited": kind in overrides.load(),
            "timing_example": _timing_example(kind, form),
            "timed_by": ""
            if form.has_timing or overrides.default(kind).anchor == "triggered"
            else overrides.timing_phrase(kind),
            "switch_label": _switch_label(kind),
        },
    )


def _timing_example(kind: str, form) -> str:
    if not form.has_timing:
        return ""
    amount = form["timing_amount"].value()
    unit = form["timing_unit"].value()
    try:
        return overrides.timing_example(kind, int(amount), unit, now=_now())
    except (TypeError, ValueError):
        return ""


@login_required
@owner_admin_required
@require_POST
def message_reset(request: HttpRequest, kind: str) -> HttpResponse:
    """Drop the owner's wording, timing and switch for a message."""
    _message_spec(kind)
    overrides.reset(kind)
    messages.success(request, "Reset to the default.")
    return redirect("message_edit", kind=kind)


@login_required
@owner_admin_required
@require_POST
def message_preview(request: HttpRequest, kind: str) -> JsonResponse:
    """A draft rendered by the real renderer against an order, or a built-in sample."""
    _message_spec(kind)
    texts = {
        name: request.POST.get(name, "").replace("\r\n", "\n").replace("\r", "\n")
        for name in MessageTemplateForm.TEXTS
    }
    errors = fields.validate(kind, **texts)
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    lead = None
    pk = request.POST.get("lead", "")
    if pk.isdigit():
        lead = (
            Lead.objects.select_related("contact")
            .prefetch_related(
                "reservations__stops",
                "reservations__vehicle",
                "reservations__service_type",
                "reservations__assignments__driver",
                "reservations__assignments__vendor",
            )
            .filter(pk=int(pk))
            .first()
        )
    rendered = overrides.preview(kind, lead, **texts)
    source = lead.quote_no if lead else "Sample order"
    return JsonResponse({"ok": True, "source": source, **rendered})


@login_required
@owner_admin_required
def message_timing_preview(request: HttpRequest, kind: str) -> JsonResponse:
    """The worked example for a timing being typed."""
    _message_spec(kind)
    try:
        text = overrides.timing_example(
            kind, int(request.GET.get("amount", "")), request.GET.get("unit", ""), now=_now()
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "text": text})


@login_required
@owner_admin_required
def pricing(request: HttpRequest) -> HttpResponse:
    """The default cost ratio used to price trips from an affiliate's rate (spec 2026-09-05)."""
    config = PricingConfig.load()
    form = PricingConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Pricing settings saved.")
        return redirect("pricing")
    return render(
        request,
        "settings/pricing.html",
        {"nav": "settings", "page_title": "Pricing", "form": form},
    )


@login_required
@owner_admin_required
def affiliate_scoring(request: HttpRequest) -> HttpResponse:
    """How the affiliate reliability score is worked out (APC-70). Read-only: the weights
    live in `settings.AFFILIATE_SCORE_WEIGHTS`, and this page is generated from the same
    rules the score uses, so it can't describe something the code doesn't do."""
    from django.conf import settings

    from apps.vendors import performance

    return render(
        request,
        "settings/affiliate_scoring.html",
        {
            "nav": "settings",
            "page_title": "Affiliate scoring",
            "rules": performance.describe(),
            "min_trips": settings.AFFILIATE_SCORE_MIN_TRIPS,
            "windows": [label for _, label in performance.WINDOWS],
            "default_window": dict(performance.WINDOWS)[performance.DEFAULT_WINDOW],
            "weak_below": performance.WEAK_BELOW,
        },
    )


@login_required
@owner_admin_required
def dispatch_alerts(request: HttpRequest) -> HttpResponse:
    """The Dispatch-alerts config (APC-23) — one singleton form, plus a live open-count."""
    config = DispatchAlertConfig.load()
    form = DispatchAlertConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Dispatch alert settings saved.")
        return redirect("dispatch_alerts")
    return render(
        request,
        "settings/dispatch_alerts.html",
        {
            "nav": "settings",
            "page_title": "Dispatch alerts",
            "form": form,
            "open_exceptions": DispatchException.objects.filter(resolved_at__isnull=True).count(),
        },
    )


@login_required
@owner_admin_required
def task_settings(request: HttpRequest) -> HttpResponse:
    """The Tasks config (APC-49) — department owners, escalation grace, digest recipients."""
    config = TaskConfig.load()
    form = TaskConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Task settings saved.")
        return redirect("task_settings")
    return render(
        request,
        "settings/tasks.html",
        {"nav": "settings", "page_title": "Tasks", "form": form},
    )


def _now() -> datetime:
    return timezone.now()


@login_required
@owner_admin_required
def checklist_schedule(request: HttpRequest) -> HttpResponse:
    """Every checklist due timing in one form (spec 2026-10-26 §1). Saving re-dates the
    open tasks of each timing that changed."""
    form = ChecklistScheduleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        count = schedule.save(form.cleaned_values(), user=request.user)
        messages.success(request, f"Saved. {count} open task{'s' if count != 1 else ''} re-dated.")
        return redirect("checklist_schedule")
    return render(
        request,
        "settings/checklist_schedule.html",
        {"nav": "settings", "page_title": "Checklist schedule", "form": form},
    )


@login_required
@owner_admin_required
def checklist_schedule_preview(request: HttpRequest) -> JsonResponse:
    """The worked example for one row, from the real due-date resolver."""
    try:
        text = schedule.preview(
            request.GET.get("key", ""),
            request.GET.get("variant", ""),
            int(request.GET.get("amount", "")),
            request.GET.get("unit", ""),
            now=_now(),
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "text": text})


# --- points of interest -------------------------------------------------------------
# The curated venue / hotel / ceremony-site directory the wedding typeaheads search
# before falling through to LocationIQ. Managed here rather than in Django admin because
# `max_vehicle` is an operational decision the office makes, not seed data.


@login_required
@owner_admin_required
def poi_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/poi_list.html",
        {
            "nav": "settings",
            "page_title": "Points of Interest",
            # select_related: the list renders every row's ceiling, which would otherwise
            # be a query per POI.
            "pois": Venue.objects.select_related("max_vehicle").order_by("name"),
        },
    )


@login_required
@owner_admin_required
def poi_create(request: HttpRequest) -> HttpResponse:
    form = VenueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Point of interest added.")
        return redirect("poi_list")
    return render(
        request,
        "settings/poi_form.html",
        {"nav": "settings", "page_title": "New point of interest", "form": form, "target": None},
    )


@login_required
@owner_admin_required
def poi_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(Venue, pk=pk)
    form = VenueForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Point of interest updated.")
        return redirect("poi_list")
    return render(
        request,
        "settings/poi_form.html",
        {"nav": "settings", "page_title": target.name, "form": form, "target": target},
    )


@login_required
@owner_admin_required
@require_POST
def poi_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Deactivate, never delete: the directory feeds typeaheads on quotes already sent,
    and `Stop` rows keep the name they were given, so a hard delete would quietly change
    what a customer's own itinerary page says."""
    target = get_object_or_404(Venue, pk=pk)
    target.is_active = False
    target.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"{target.name} deactivated.")
    return redirect("poi_list")


# --- QuickBooks Online (APC-41) ----------------------------------------------------------------

QBO_STATE_KEY = "qbo_oauth_state"


def _qbo_or_404():
    from django.http import Http404

    from apps.integrations import qbo

    if not qbo.enabled():
        raise Http404
    return qbo


@login_required
@owner_admin_required
def quickbooks_settings(request: HttpRequest) -> HttpResponse:
    """Connect / reconnect / disconnect, which company, when, by whom, the last good call.
    Tokens are never rendered. 404 while the client keys are blank — the feature is dark."""
    from django.conf import settings as dj_settings

    from apps.billing import qbo_sync
    from apps.billing.models import QboTerm
    from apps.integrations.models import QboCredential

    qbo = _qbo_or_404()
    cred = QboCredential.objects.filter(singleton_id=1).select_related("connected_by").first()
    return render(
        request,
        "settings/quickbooks.html",
        {
            "nav": "settings",
            "page_title": "QuickBooks",
            "cred": cred if cred and cred.realm_id else None,
            "connected": qbo.is_connected(),
            "environment": dj_settings.QBO_ENVIRONMENT,
            # APC-42: the company's terms we match against, and ours that it lacks.
            "qbo_terms": list(QboTerm.objects.filter(active=True)),
            "unmatched_terms": qbo_sync.unmatched_terms(),
            **_qbo_accounts_context(),
        },
    )


def _qbo_accounts_context() -> dict:
    """APC-74: the two account choices for affiliate bills, offered from the cached chart of
    accounts. A choice that's no longer an active account of this company reads as unset."""
    from apps.billing.models import QboAccountMapping
    from apps.payments import qbo_bills

    config = QboAccountMapping.load()
    expense = config.expense_account_id if qbo_bills.expense_account_ref() else ""
    payment = config.payment_account_id if qbo_bills.payment_account_ref() else ""
    return {
        "expense_choices": qbo_bills.expense_choices(),
        "payment_choices": qbo_bills.payment_choices(),
        "expense_selected": expense,
        "payment_selected": payment,
        "bills_from": config.bills_from,
        "accounts_unset": not (expense and payment and config.bills_from),
    }


@login_required
@owner_admin_required
@require_POST
def quickbooks_accounts(request: HttpRequest) -> HttpResponse:
    """Save which accounts affiliate bills post to and pay from, and the approval date they
    start from. Only an active account of the right kind in the connected company is accepted;
    anything else (or a date that doesn't parse) is left unset."""
    from django.utils.dateparse import parse_date

    from apps.billing.models import QboAccount, QboAccountMapping
    from apps.payments import qbo_bills

    _qbo_or_404()

    def pick(field: str, choices: list[tuple[int, str]]):
        raw = request.POST.get(field, "")
        allowed = {str(pk) for pk, _ in choices}
        return QboAccount.objects.get(pk=raw) if raw in allowed else None

    config = QboAccountMapping.load()
    config.expense_account = pick("expense_account", qbo_bills.expense_choices())
    config.payment_account = pick("payment_account", qbo_bills.payment_choices())
    config.bills_from = parse_date(request.POST.get("bills_from", "").strip() or "") or None
    config.save()
    if config.expense_account and config.payment_account and config.bills_from:
        messages.success(request, "Saved. Waiting bills go out on the next sync.")
    else:
        messages.error(
            request, "Choose an expense account, a bank account and the date bills start from."
        )
    return redirect("quickbooks_settings")


@login_required
@owner_admin_required
@require_POST
def quickbooks_refresh_accounts(request: HttpRequest) -> HttpResponse:
    """Re-read the company's expense and bank accounts now (the hourly sync does it too)."""
    from apps.payments import qbo_bills

    qbo = _qbo_or_404()
    if not qbo.is_connected():
        messages.error(request, "Connect QuickBooks first.")
        return redirect("quickbooks_settings")
    try:
        count = qbo_bills.refresh_accounts()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        messages.error(request, "Couldn't read QuickBooks' accounts. Try again in a minute.")
    else:
        messages.success(request, f"{count} expense and bank accounts read from QuickBooks.")
    return redirect("quickbooks_settings")


@login_required
@owner_admin_required
@require_POST
def quickbooks_connect(request: HttpRequest) -> HttpResponse:
    """Start Intuit's consent. The state lives in the session the callback reads, so start
    this from the redirect URI's own domain (the ngrok host in dev), never localhost."""
    import secrets

    qbo = _qbo_or_404()
    state = secrets.token_urlsafe(24)
    request.session[QBO_STATE_KEY] = state
    return redirect(qbo.authorize_url(state))


@login_required
@owner_admin_required
@require_POST
def quickbooks_refresh_terms(request: HttpRequest) -> HttpResponse:
    """Re-read the company's payment terms now (the hourly sync does it too)."""
    from apps.billing import qbo_sync

    qbo = _qbo_or_404()
    if not qbo.is_connected():
        messages.error(request, "Connect QuickBooks first.")
        return redirect("quickbooks_settings")
    try:
        count = qbo_sync.refresh_terms()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        messages.error(request, "Couldn't read QuickBooks' terms. Try again in a minute.")
    else:
        messages.success(request, f"{count} payment terms read from QuickBooks.")
    return redirect("quickbooks_settings")


@login_required
@owner_admin_required
@require_POST
def quickbooks_disconnect(request: HttpRequest) -> HttpResponse:
    qbo = _qbo_or_404()
    qbo.disconnect()
    messages.success(request, "QuickBooks disconnected.")
    return redirect("quickbooks_settings")


@login_required
@owner_admin_required
def document_type_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "settings/document_type_list.html",
        {
            "nav": "settings",
            "page_title": "Affiliate documents",
            "document_types": AffiliateDocumentType.objects.all(),
        },
    )


@login_required
@owner_admin_required
def document_type_create(request: HttpRequest) -> HttpResponse:
    form = AffiliateDocumentTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Document type added.")
        return redirect("document_type_list")
    return render(
        request,
        "settings/document_type_form.html",
        {"nav": "settings", "page_title": "New document type", "form": form, "target": None},
    )


@login_required
@owner_admin_required
def document_type_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(AffiliateDocumentType, pk=pk)
    form = AffiliateDocumentTypeForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Document type updated.")
        return redirect("document_type_list")
    return render(
        request,
        "settings/document_type_form.html",
        {"nav": "settings", "page_title": target.name, "form": form, "target": target},
    )


@login_required
@owner_admin_required
@require_POST
def document_type_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Deactivate when documents reference the type (PROTECT). "Other" is the upload
    default, so it can't go at all."""
    target = get_object_or_404(AffiliateDocumentType, pk=pk)
    if target.name == "Other":
        messages.error(request, "Other is the default for uploads and can't be removed.")
    elif target.documents.exists():
        target.active = False
        target.save(update_fields=["active"])
        messages.success(request, f"{target.name} deactivated (it has documents).")
    else:
        name = target.name
        target.delete()
        messages.success(request, f"{name} deleted.")
    return redirect("document_type_list")
