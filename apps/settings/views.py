"""Settings screens — owner-admin only."""

from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.accounts.permissions import owner_admin_required
from apps.addresses.models import Venue
from apps.dispatch.models import DispatchAlertConfig, DispatchException
from apps.fleet.forms import RenewalTypeForm
from apps.fleet.models import RENEWAL_PREFETCH, Driver, RenewalType, Vehicle
from apps.leads.models import Lead, ServiceType, VehicleType
from apps.messaging import defaults, editor, fields, listing, recipients, rules, triggers
from apps.messaging.models import MessageTemplate, NotificationConfig
from apps.reservations.models import PricingConfig
from apps.tasks import schedule
from apps.tasks.models import TaskConfig
from apps.vendors.models import AffiliateDocumentType

from .forms import (
    AffiliateDocumentTypeForm,
    ChecklistScheduleForm,
    DispatchAlertConfigForm,
    MessageForm,
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
    silently blank the service on historical quotes. Refused while a message names it:
    dropped from the message's service types, the message would go for every trip."""
    target = get_object_or_404(ServiceType, pk=pk)
    using = list(
        MessageTemplate.objects.filter(service_types=target)
        .order_by("name")
        .values_list("name", flat=True)
    )
    if target.reservation_set.exists():
        target.active = False
        target.save(update_fields=["active", "updated_at"])
        messages.success(request, f"{target.name} deactivated (it's used by existing trips).")
    elif using:
        messages.error(
            request,
            f"{target.name} can't be deleted while messages use it: {', '.join(using)}. "
            "Take it off those messages first.",
        )
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


_SEGMENTS = (("", "All"), ("on", "On"), ("off", "Off"))


def _filter_url(filters: dict, **changes: str) -> str:
    merged = {**filters, **changes}
    query = urlencode({k: v for k, v in merged.items() if v})
    return f"{reverse('messages')}?{query}" if query else reverse("messages")


@login_required
@owner_admin_required
def messages_list(request: HttpRequest) -> HttpResponse:
    """Every automated message in its section, problems first, filtered from the query string."""
    config = NotificationConfig.load()
    master = config.enabled
    filters = {key: request.GET.get(key, "").strip() for key in listing.FILTER_KEYS}
    rows = MessageTemplate.objects.live().prefetch_related("service_types")
    sections = listing.build_sections(rows, filters, master_on=master)
    filtering = any(filters.values())
    group_order = {kind: i for i, kind in enumerate(triggers.GROUP_LABEL)}
    triggers_by_group = sorted(
        triggers.CATALOG.values(),
        key=lambda t: (group_order.get(t.kind, len(group_order)), t.label),
    )
    return render(
        request,
        "settings/messages.html",
        {
            "nav": "settings",
            "page_title": "Messages",
            "sections": sections,
            "master_on": master,
            "quiet_label": quiet_hours_label(config),
            "quiet_start": config.quiet_start.strftime("%H:%M"),
            "quiet_end": config.quiet_end.strftime("%H:%M"),
            "filters": filters,
            "filtering": filtering,
            "audience_options": MessageTemplate.Audience.choices,
            "channel_options": [("sms", "Text"), ("email", "Email"), ("both", "Text and Email")],
            "trigger_options": [
                (t.key, f"{triggers.GROUP_LABEL.get(t.kind, t.kind)}: {t.label}")
                for t in triggers_by_group
            ],
            "service_options": list(ServiceType.objects.order_by("name").values_list("pk", "name")),
            "origin_options": [("seeded", "Built-in"), ("added", "Added")],
            "segments": [
                {
                    "label": label,
                    "active": filters["state"] == value,
                    "url": _filter_url(filters, state=value),
                }
                for value, label in _SEGMENTS
            ],
            "clear_url": reverse("messages"),
        },
    )


def _clock(value: time) -> str:
    """9 PM, 7:30 AM: the way the header and the picker read a time."""
    text = value.strftime("%I:%M %p").lstrip("0")
    return text.replace(":00", "")


def quiet_hours_label(config: NotificationConfig) -> str:
    """The Messages header text for the saved window; equal ends mean it is off."""
    if config.quiet_start == config.quiet_end:
        return "Quiet hours off"
    return f"Quiet hours {_clock(config.quiet_start)} to {_clock(config.quiet_end)}"


def _parse_clock(raw: str) -> time | None:
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except ValueError:
        return None


@login_required
@owner_admin_required
@require_POST
def messages_quiet_hours(request: HttpRequest) -> JsonResponse:
    """Save the window customer texts wait out; start equal to end switches it off."""
    start = _parse_clock(request.POST.get("start", ""))
    end = _parse_clock(request.POST.get("end", ""))
    if start is None or end is None:
        return JsonResponse({"ok": False, "error": "Pick a start and an end time."}, status=400)
    config = NotificationConfig.load()
    config.quiet_start, config.quiet_end = start, end
    config.save(update_fields=["quiet_start", "quiet_end"])
    return JsonResponse({"ok": True, "label": quiet_hours_label(config)})


# The section a new message starts in also picks its audience and a trigger that suits it.
_NEW_IN_SECTION = {
    "leads_quotes": ("customer", "lead_created"),
    "booking_payments": ("customer", "order_confirmed"),
    "trip_day": ("customer", "pickup"),
    "after_trip": ("customer", "all_trips_done"),
    "affiliates": ("affiliate", "affiliate_accepted"),
    # Not a staff click or a schedule: an added row can't be sent on those (`editor.can_pick`).
    "planners": ("planner", "quote_sent"),
    "staff_reports": ("staff", "lead_created"),
}


def _live_row(key: str) -> MessageTemplate:
    return get_object_or_404(
        MessageTemplate.objects.live().prefetch_related("service_types"), key=key
    )


@login_required
@owner_admin_required
def messages_new(request: HttpRequest) -> HttpResponse:
    """The editor for a message an Admin adds; its section (and so its audience and a
    fitting trigger) preset from `?section=`."""
    section = request.GET.get("section", "")
    if section not in _NEW_IN_SECTION:
        section = MessageTemplate.Section.LEADS_QUOTES
    audience, trigger = _NEW_IN_SECTION[section]
    row = MessageTemplate(
        section=section,
        audience=audience,
        trigger=trigger,
        send_sms=audience != MessageTemplate.Audience.STAFF,
        recipients=["booking_contact"] if audience == MessageTemplate.Audience.CUSTOMER else [],
        seeded=False,
        enabled=True,
    )
    return _editor(request, row)


@login_required
@owner_admin_required
def message_edit(request: HttpRequest, key: str) -> HttpResponse:
    """One message: who gets it, when, for which service types and what it says, with a live
    preview against a real order."""
    return _editor(request, _live_row(key))


def _editor(request: HttpRequest, row: MessageTemplate) -> HttpResponse:
    new = row.pk is None
    title = row.name
    form = MessageForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        count = form.save(request.user)
        if new:
            messages.success(request, f"{row.name} added.")
        else:
            plural = "s" if count != 1 else ""
            note = f" {count} scheduled message{plural} re-dated." if count else ""
            messages.success(request, f"Saved. Applies to messages not yet sent.{note}")
        return redirect("messages")
    return render(request, "settings/message_edit.html", _editor_context(form, row, title, new))


def _order_label(lead) -> str:
    pickups = [r.pickup_date for r in lead.reservations.all() if r.pickup_date]
    first = f"{min(pickups):%b %-d, %Y}" if pickups else "no date"
    return f"{lead.quote_no} · {lead.contact.name} · {first}"


def _chips(trigger_key: str, audience: str, row: MessageTemplate) -> list[dict]:
    if trigger_key not in triggers.CATALOG:
        return []
    key = row.key if row.seeded else ""
    return [
        {"key": f.key, "label": f.label, "group": f.group}
        for f in fields.fields_for(trigger_key, audience or "customer", key)
    ]


def _trigger_meta() -> dict:
    """What the page needs to know about each trigger to react as the dropdown changes."""
    return {
        t.key: {
            "label": t.label,
            "kind": t.kind,
            "date": t.kind == "date",
            "timing": editor.has_timing(t.key),
            "click": t.kind == "staff_click",
            "audiences": [a for a in MessageTemplate.Audience.values if a in t.audiences],
            "anchor": editor.anchor_noun(t.key),
            "behaviour": editor.behaviour(t.key),
            "combineLine": editor.COMBINE_LINE if t.combine else "",
            "untimed": _untimed_text(t.key),
        }
        for t in triggers.CATALOG.values()
    }


def _untimed_text(trigger_key: str) -> str:
    trigger = triggers.CATALOG.get(trigger_key)
    if trigger is None or editor.has_timing(trigger_key):
        return ""
    if trigger.kind == "staff_click":
        return f"Sent when staff click {trigger.label}"
    if trigger.caller_sends:
        return f"Sent the moment it happens ({trigger.label.lower()})"
    return f"Sent on its schedule ({trigger.label.lower()})"


def _trigger_options(row: MessageTemplate, current: str) -> list[dict]:
    """The triggers this row may be put on (`editor.can_pick`), and the one it is on."""
    group_order = {kind: i for i, kind in enumerate(triggers.GROUP_LABEL)}
    ordered = sorted(
        (t for t in triggers.CATALOG.values() if t.key == current or editor.can_pick(row, t.key)),
        key=lambda t: (group_order.get(t.kind, len(group_order)), t.label),
    )
    return [
        {"value": t.key, "label": f"{triggers.GROUP_LABEL.get(t.kind, t.kind)}: {t.label}"}
        for t in ordered
    ]


def _editor_context(form: MessageForm, row: MessageTemplate, title: str, new: bool) -> dict:
    trigger_key = form["trigger"].value() or ""
    audience = form["audience"].value() or ""
    trigger = triggers.CATALOG.get(trigger_key)
    default = defaults.ORIGINALS.get(row.key) if row.seeded else None
    original = default.trigger if default else ""
    stored = MessageTemplate.objects.prefetch_related("service_types").filter(pk=row.pk).first()
    types = list(stored.service_types.all()) if stored else []
    orders = (
        editor.preview_orders(trigger_key, types, key=row.key if row.seeded else "")
        if trigger
        else []
    )
    picked = [str(v) for v in form["recipients"].value() or []]
    services = [str(v) for v in form["service_types"].value() or []]
    timing_amount = form["timing_amount"].value()
    try:
        example = editor.timing_example(
            trigger_key,
            int(timing_amount),
            form["timing_unit"].value() or "",
            form["timing_direction"].value() or "after",
            now=_now(),
        )
    except (TypeError, ValueError):
        example = ""
    state = {
        "trigger": trigger_key,
        "audience": audience,
        "direction": form["timing_direction"].value() or "after",
        "sms": bool(form["send_sms"].value()),
        "email": bool(form["send_email"].value()),
        "enabled": bool(form["enabled"].value()),
        "picked": picked,
        "services": services,
        "originalTrigger": original,
        "triggers": _trigger_meta(),
        "labels": recipients.LABELS,
        "audienceKeys": {a: recipients.keys_for(a) for a in MessageTemplate.Audience.values},
        "textOnly": sorted(recipients.TEXT_ONLY),
        "staffEmails": list(form["staff_emails"].value() or []),
        "chips": _chips(trigger_key, audience, row),
        "previewUrl": reverse("message_preview", args=[row.key])
        if not new
        else reverse("messages_new_preview"),
        "timingUrl": reverse("message_timing_preview", args=[row.key])
        if not new
        else reverse("messages_new_timing"),
    }
    locked, why = editor.locked_fields(row)
    if locked:
        # What the button or report sets, as stored: a refused change is never shown as if
        # it stuck.
        state |= {
            "trigger": row.trigger,
            "audience": row.audience,
            "sms": row.send_sms,
            "email": row.send_email,
            "picked": list(row.recipients or []),
        }
    built_ins = rules.built_in_descriptions(row.key) if row.seeded else []
    changed = bool(original and trigger_key != original)
    state["builtIns"] = built_ins
    state["perTrip"] = bool(row.seeded and rules.combines_per_trip(row.key))
    # Whether the draft sends one message per person per day (its wording decides, for a
    # row without built-in rules); the page updates it from each preview.
    state["combines"] = rules.combines_per_day(
        MessageTemplate(
            key=row.key,
            seeded=row.seeded,
            trigger=trigger_key,
            audience=audience,
            send_sms=state["sms"],
            send_email=state["email"],
            subject=form["subject"].value() or "",
            email_body=form["email_body"].value() or "",
            sms_body=form["sms_body"].value() or "",
        )
    )
    direction = state["direction"]
    section = form["section"].value() or row.section
    errors = {name: form.errors[name][0] for name in form.errors}
    return {
        "nav": "settings",
        "page_title": title or "New message",
        "title": title or "New message",
        "new": new,
        "row": row,
        "form": form,
        "errors": errors,
        "state": state,
        "tag": listing.tag(stored) if stored else "",
        "section_label": (
            MessageTemplate.Section(section).label
            if section in MessageTemplate.Section.values
            else ""
        ),
        "section_options": MessageTemplate.Section.choices,
        "trigger_options": _trigger_options(row, trigger_key),
        "locked": editor.is_locked(row),
        "recipients_locked": editor.recipients_locked(row),
        "report_row": editor.is_report(row),
        "click_who": editor.WHO.get(row.key, "") if editor.is_locked(row) else "",
        "locked_error": next(
            (
                form.errors[name][0]
                for name in (*locked, "service_types")
                if name in form.errors and form.errors[name][0] == why
            ),
            "",
        ),
        "stored_recipients": (
            list(stored.recipients or []) if editor.recipients_locked(row) else []
        ),
        "stored_services": [st.pk for st in types] if locked else [],
        "unit_options": [(u, u) for u in editor.TIMING_UNITS],
        "audience_options": MessageTemplate.Audience.choices,
        "recipient_groups": [
            (audience_key, groups) for audience_key, groups in recipients.CHOICES.items() if groups
        ],
        "text_only": recipients.TEXT_ONLY,
        "service_options": list(form.fields["service_types"].queryset),
        "picked": picked,
        "services": services,
        "has_timing": editor.has_timing(trigger_key),
        "is_date": bool(trigger and trigger.kind == "date"),
        "is_click": bool(trigger and trigger.kind == "staff_click"),
        "click_label": trigger.label if trigger and trigger.kind == "staff_click" else "",
        "anchor_word": (
            f"{direction} {editor.anchor_noun(trigger_key)}"
            if trigger and trigger.kind == "date"
            else "after it happens"
        ),
        "no_timing_text": _untimed_text(trigger_key),
        "direction_options": MessageTemplate.Direction.choices,
        "rule_lines": editor.behaviour(trigger_key, combines=state["combines"])
        + ([] if changed else built_ins),
        "built_ins": built_ins,
        "trigger_changed": changed,
        "timing_example": example,
        "orders": [(lead.pk, _order_label(lead)) for lead in orders],
        "default_order": orders[0].pk if orders else "",
    }


@login_required
@owner_admin_required
@require_POST
def message_archive(request: HttpRequest, key: str) -> HttpResponse:
    """Archive an added message. Built-in messages are never archived (only reset), so
    there is no archive for them."""
    row = get_object_or_404(MessageTemplate.objects.live(), key=key, seeded=False)
    editor.archive(row, user=request.user)
    messages.success(request, f"{row.name} archived.")
    return redirect("messages")


@login_required
@owner_admin_required
@require_POST
def message_reset(request: HttpRequest, key: str) -> HttpResponse:
    """Put a built-in message back to its original wording, timing, people and switch."""
    row = get_object_or_404(MessageTemplate.objects.live(), key=key, seeded=True)
    editor.reset(row, user=request.user)
    messages.success(request, "Reset to the original.")
    return redirect("message_edit", key=key)


@login_required
@owner_admin_required
@require_POST
def message_toggle(request: HttpRequest, key: str) -> JsonResponse:
    """Flip one message's switch, leaving the rest as it is. Staff clicks have no switch, and
    a message whose wording can't render is fixed in the editor first."""
    row = _live_row(key)
    if editor.is_staff_click(row.trigger):
        return JsonResponse({"ok": False, "error": "This one has no switch."}, status=400)
    broken = fields.wording_errors(listing.row_data(row))
    if broken:
        return JsonResponse({"ok": False, "error": next(iter(broken.values()))}, status=400)
    editor.set_enabled(row, not row.enabled, user=request.user)
    return JsonResponse({"ok": True, "enabled": row.enabled})


@login_required
@owner_admin_required
@require_POST
def messages_master(request: HttpRequest) -> JsonResponse:
    """Flip the master switch that stops every automated message."""
    config = NotificationConfig.load()
    config.enabled = not config.enabled
    config.save(update_fields=["enabled"])
    return JsonResponse({"ok": True, "enabled": config.enabled})


def _preview_lead(pk: str):
    if not pk.isdigit():
        return None
    return (
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


@login_required
@owner_admin_required
@require_POST
def message_preview(request: HttpRequest, key: str | None = None) -> JsonResponse:
    """The draft as posted (the whole editor form), rendered by the real renderer against an
    order or a built-in sample, with whom it would reach and the fields its trigger can fill.
    Sends nothing."""
    row = _live_row(key) if key else MessageTemplate(seeded=False)
    stored_types = list(row.service_types.all()) if row.pk else []
    form = MessageForm(request.POST, instance=row)
    form.is_valid()
    draft = form.instance
    chips = _chips(draft.trigger, draft.audience, row)
    errors = {n: form.errors[n][0] for n in MessageForm.TEXTS if n in form.errors}
    if errors:
        return JsonResponse({"ok": False, "errors": errors, "fields": chips}, status=400)
    if draft.trigger not in triggers.CATALOG:
        return JsonResponse({"ok": False, "errors": {"trigger": "Pick a trigger."}}, status=400)
    types = form.cleaned_data.get("service_types", stored_types)
    lead = _preview_lead(request.POST.get("lead", ""))
    rendered = editor.preview(draft, lead, service_types=types)
    source = lead.quote_no if lead else "Sample order"
    return JsonResponse(
        {
            "ok": True,
            "source": source,
            "fields": chips,
            "combines": rules.combines_per_day(draft),
            **rendered,
        }
    )


@login_required
@owner_admin_required
def message_timing_preview(request: HttpRequest, key: str | None = None) -> JsonResponse:
    """The worked example for a timing being typed (trigger and direction default to the
    row's)."""
    row = _live_row(key) if key else None
    trigger = request.GET.get("trigger") or (row.trigger if row else "")
    direction = request.GET.get("direction") or (row.timing_direction if row else "after")
    try:
        text = editor.timing_example(
            trigger,
            int(request.GET.get("amount", "")),
            request.GET.get("unit", ""),
            direction,
            now=_now(),
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
            **_qbo_customer_docs_context(),
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


def _qbo_customer_docs_context() -> dict:
    """APC-107: the Product/Service every trip line posts to and the date customer documents
    start from. A choice that's no longer an active item of this company reads as unset."""
    from apps.billing import qbo_invoices
    from apps.billing.models import QboAccountMapping

    config = QboAccountMapping.load()
    item = config.trip_item_id if qbo_invoices.trip_item_ref() else ""
    return {
        "item_choices": qbo_invoices.item_choices(),
        "trip_item_selected": item,
        "customer_docs_from": config.customer_docs_from,
        "customer_docs_unset": not (item and config.customer_docs_from),
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
def quickbooks_customer_docs(request: HttpRequest) -> HttpResponse:
    """Save the Product/Service trip lines post to and the date customer documents start from.
    Only an active item of the connected company is accepted; anything else (or a date that
    doesn't parse) is left unset."""
    from django.utils.dateparse import parse_date

    from apps.billing import qbo_invoices
    from apps.billing.models import QboAccountMapping, QboItem

    _qbo_or_404()
    raw = request.POST.get("trip_item", "")
    allowed = {str(pk) for pk, _ in qbo_invoices.item_choices()}
    config = QboAccountMapping.load()
    config.trip_item = QboItem.objects.get(pk=raw) if raw in allowed else None
    try:
        config.customer_docs_from = parse_date(request.POST.get("customer_docs_from", "").strip())
    except ValueError:  # well-formed but impossible, like Feb 30
        config.customer_docs_from = None
    config.save()
    if config.trip_item and config.customer_docs_from:
        messages.success(request, "Saved. Open invoices go out on the next sync.")
    else:
        messages.error(
            request,
            "Choose the Product/Service for trip charges and the date customer documents "
            "start from.",
        )
    return redirect("quickbooks_settings")


@login_required
@owner_admin_required
@require_POST
def quickbooks_refresh_items(request: HttpRequest) -> HttpResponse:
    """Re-read the company's products and services now (the hourly sync does it too)."""
    from apps.billing import qbo_invoices

    qbo = _qbo_or_404()
    if not qbo.is_connected():
        messages.error(request, "Connect QuickBooks first.")
        return redirect("quickbooks_settings")
    try:
        count = qbo_invoices.refresh_items()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        messages.error(
            request, "Couldn't read QuickBooks' products and services. Try again in a minute."
        )
    else:
        messages.success(request, f"{count} products and services read from QuickBooks.")
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
