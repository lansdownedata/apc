"""Settings screens — owner-admin only."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.permissions import owner_admin_required
from apps.leads.models import VehicleType

from .forms import VehicleTypeForm


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
    """Deactivate rather than delete when reservations reference this type — the FK is
    SET_NULL, so a hard delete would blank the vehicle on historical quotes."""
    target = get_object_or_404(VehicleType, pk=pk)
    if target.reservation_set.exists():
        target.active = False
        target.save(update_fields=["active", "updated_at"])
        messages.success(request, f"{target.name} deactivated (it's used by existing trips).")
    else:
        name = target.name
        target.delete()
        messages.success(request, f"{name} deleted.")
    return redirect("vehicle_type_list")
