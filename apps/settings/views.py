"""Settings screens — owner-admin only."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.permissions import owner_admin_required
from apps.addresses.models import Venue
from apps.dispatch.models import DispatchAlertConfig, DispatchException
from apps.fleet.forms import RenewalTypeForm
from apps.fleet.models import RENEWAL_PREFETCH, Driver, RenewalType, Vehicle
from apps.leads.models import ServiceType, VehicleType
from apps.messaging.models import NotificationConfig
from apps.reservations.models import PricingConfig
from apps.tasks.models import TaskConfig

from .forms import (
    DispatchAlertConfigForm,
    NotificationConfigForm,
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


@login_required
@owner_admin_required
def notifications(request: HttpRequest) -> HttpResponse:
    """The reservation-lifecycle messaging switches (APC-18-22) — one singleton form."""
    config = NotificationConfig.load()
    form = NotificationConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Notification settings saved.")
        return redirect("notifications")
    return render(
        request,
        "settings/notifications.html",
        {"nav": "settings", "page_title": "Customer notifications", "form": form},
    )


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
        },
    )


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
