"""Fleet — in-house drivers, units, and their renewals. All views staff-only."""

from __future__ import annotations

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import CharField, Q
from django.db.models.functions import Cast
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.addresses.models import Address
from apps.addresses.smart_address import apply_posted_address

from .forms import DriverForm, VehicleForm
from .models import RENEWAL_PREFETCH, RENEWAL_SEVERITY, Driver, Vehicle

_SEVERITY_RANK = {s: i for i, s in enumerate(RENEWAL_SEVERITY)}
_STATUS_FILTERS = [("active", "Active"), ("inactive", "Inactive"), ("all", "All")]


def _status_filter(request: HttpRequest) -> str:
    value = (request.GET.get("status") or "active").strip()
    return value if value in {"active", "inactive", "all"} else "active"


def _split(rows: list, status_filter: str) -> tuple[list, list, int]:
    """Attention-first split, the same shape as vendor_list: active subjects with lapsing
    paperwork first (worst, then soonest), then the healthy roster; inactive subjects are
    hidden unless the filter asks for them. Returns (attention, roster, hidden_count)."""
    for row in rows:
        row.summary = row.renewal_summary()
    active = [r for r in rows if r.status == "active"]
    inactive = sorted((r for r in rows if r.status != "active"), key=lambda r: r.name.lower())

    def attn_key(row):
        days = row.summary["days"]
        return (
            _SEVERITY_RANK.get(row.summary["status"], len(RENEWAL_SEVERITY)),
            days if days is not None else 10**6,
            row.name.lower(),
        )

    attention = sorted((r for r in active if r.needs_attention), key=attn_key)
    healthy = sorted((r for r in active if not r.needs_attention), key=lambda r: r.name.lower())
    if status_filter == "inactive":
        return [], inactive, 0
    if status_filter == "all":
        return attention, healthy + inactive, 0
    return attention, healthy, len(inactive)


def _list_context(request: HttpRequest, *, tab: str, rows: list, query: str) -> dict:
    status_filter = _status_filter(request)
    attention, roster, hidden = _split(rows, status_filter)
    return {
        "nav": "fleet",
        "page_title": "Fleet",
        "tab": tab,
        "attention": attention,
        "roster": roster,
        "hidden_count": hidden,
        "active_count": sum(1 for r in rows if r.status == "active"),
        "q": query,
        "status_filter": status_filter,
        "status_options": _STATUS_FILTERS,
    }


@login_required
def driver_list(request: HttpRequest) -> HttpResponse:
    drivers = Driver.objects.prefetch_related(RENEWAL_PREFETCH)
    query = (request.GET.get("q") or "").strip()
    if query:
        lookup = Q(name__icontains=query) | Q(email__icontains=query)
        digits = re.sub(r"\D", "", query)
        if digits:
            # Cast, not `driver_number__contains`: LIKE on an integer column is a MySQL-only
            # implicit cast and errors on Postgres (prod).
            drivers = drivers.annotate(number_text=Cast("driver_number", output_field=CharField()))
            lookup |= Q(number_text__contains=digits)
        if len(digits) >= 3:
            lookup |= Q(phone__icontains=digits)
        drivers = drivers.filter(lookup)
    return render(
        request,
        "fleet/driver_list.html",
        _list_context(request, tab="drivers", rows=list(drivers), query=query),
    )


@login_required
def vehicle_list(request: HttpRequest) -> HttpResponse:
    vehicles = Vehicle.objects.select_related("vehicle_type").prefetch_related(RENEWAL_PREFETCH)
    query = (request.GET.get("q") or "").strip()
    if query:
        vehicles = vehicles.filter(
            Q(name__icontains=query)
            | Q(license_plate__icontains=query)
            | Q(make__icontains=query)
            | Q(model_name__icontains=query)
        )
    return render(
        request,
        "fleet/vehicle_list.html",
        _list_context(request, tab="vehicles", rows=list(vehicles), query=query),
    )


def _form_page(
    request: HttpRequest,
    *,
    form,
    title: str,
    back_url: str,
    back_label: str,
    submit_label: str,
    target=None,
) -> HttpResponse:
    return render(
        request,
        "fleet/form.html",
        {
            "nav": "fleet",
            "page_title": title,
            "form": form,
            "back_url": back_url,
            "back_label": back_label,
            "submit_label": submit_label,
            "target": target,
        },
    )


@login_required
def driver_create(request: HttpRequest) -> HttpResponse:
    form = DriverForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        driver = form.save()
        messages.success(request, f"Driver {driver.driver_number} added.")
        return redirect("fleet:driver_detail", pk=driver.pk)
    return _form_page(
        request,
        form=form,
        title="New driver",
        back_url=reverse("fleet:driver_list"),
        back_label="Drivers",
        submit_label="Save driver",
    )


@login_required
def driver_edit(request: HttpRequest, pk: int) -> HttpResponse:
    driver = get_object_or_404(Driver, pk=pk)
    form = DriverForm(request.POST or None, instance=driver)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Driver updated.")
        return redirect("fleet:driver_detail", pk=driver.pk)
    return _form_page(
        request,
        form=form,
        title=f"{driver.driver_number} · {driver.name}",
        back_url=reverse("fleet:driver_detail", args=[driver.pk]),
        back_label=driver.name,
        submit_label="Save driver",
        target=driver,
    )


def _split_renewals(subject) -> tuple[list, list]:
    current = subject.current_renewals
    current_ids = {r.pk for r in current}
    history = [r for r in subject.renewals.all() if r.pk not in current_ids]
    return current, history


@login_required
def driver_detail(request: HttpRequest, pk: int) -> HttpResponse:
    driver = get_object_or_404(
        Driver.objects.select_related("home_address").prefetch_related(RENEWAL_PREFETCH), pk=pk
    )
    current, history = _split_renewals(driver)
    return render(
        request,
        "fleet/driver_detail.html",
        {
            "nav": "fleet",
            "page_title": driver.name,
            "driver": driver,
            "summary": driver.renewal_summary(),
            "current": current,
            "history": history,
            "addr_url": reverse("fleet:driver_address_update", args=[driver.pk]),
            "ac_url": reverse("integrations:geocode_autocomplete"),
        },
    )


@login_required
@require_POST
def driver_address_update(request: HttpRequest, pk: int) -> JsonResponse:
    """Auto-save endpoint for the smart-address widget — lazily creates the driver's home
    Address on first save (mirrors vendor_address_update)."""
    driver = get_object_or_404(Driver, pk=pk)
    address = driver.home_address
    if address is None:
        address = Address.objects.create()
        driver.home_address = address
        driver.save(update_fields=["home_address", "updated_at"])
    apply_posted_address(address, request.POST)
    return JsonResponse({"ok": True, "address_id": address.pk})


@login_required
def vehicle_create(request: HttpRequest) -> HttpResponse:
    form = VehicleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        vehicle = form.save()
        messages.success(request, f"{vehicle.name} added.")
        return redirect("fleet:vehicle_detail", pk=vehicle.pk)
    return _form_page(
        request,
        form=form,
        title="New vehicle",
        back_url=reverse("fleet:vehicle_list"),
        back_label="Vehicles",
        submit_label="Save vehicle",
    )


@login_required
def vehicle_edit(request: HttpRequest, pk: int) -> HttpResponse:
    vehicle = get_object_or_404(Vehicle, pk=pk)
    form = VehicleForm(request.POST or None, instance=vehicle)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Vehicle updated.")
        return redirect("fleet:vehicle_detail", pk=vehicle.pk)
    return _form_page(
        request,
        form=form,
        title=vehicle.name,
        back_url=reverse("fleet:vehicle_detail", args=[vehicle.pk]),
        back_label=vehicle.name,
        submit_label="Save vehicle",
        target=vehicle,
    )


@login_required
def vehicle_detail(request: HttpRequest, pk: int) -> HttpResponse:
    vehicle = get_object_or_404(
        Vehicle.objects.select_related("vehicle_type").prefetch_related(RENEWAL_PREFETCH), pk=pk
    )
    current, history = _split_renewals(vehicle)
    return render(
        request,
        "fleet/vehicle_detail.html",
        {
            "nav": "fleet",
            "page_title": vehicle.name,
            "vehicle": vehicle,
            "summary": vehicle.renewal_summary(),
            "current": current,
            "history": history,
        },
    )
