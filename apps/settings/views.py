"""Settings screens — owner-admin only."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.permissions import owner_admin_required
from apps.dispatch.models import DispatchAlertConfig, DispatchException
from apps.fleet.forms import RenewalTypeForm
from apps.fleet.models import RENEWAL_PREFETCH, Driver, RenewalType, Vehicle
from apps.leads.models import ServiceType, VehicleType
from apps.messaging.models import NotificationConfig

from .forms import (
    DispatchAlertConfigForm,
    NotificationConfigForm,
    ServiceTypeForm,
    VehicleTypeForm,
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
            "renewal_type_count": RenewalType.objects.filter(active=True).count(),
            "fleet_attention_count": _fleet_attention_count(),
            "dispatch_monitor_on": DispatchAlertConfig.load().enabled,
            "open_dispatch_exceptions": DispatchException.objects.filter(
                resolved_at__isnull=True
            ).count(),
            "notifications_on": NotificationConfig.load().enabled,
        },
    )


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
