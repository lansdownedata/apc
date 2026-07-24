"""Vendor management — directory, detail, and CRUD."""

from __future__ import annotations

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import VendorDocumentForm, VendorDriverForm, VendorForm, VendorInsuranceForm
from .models import (
    INSURANCE_SEVERITY,
    Vendor,
    VendorDocument,
    VendorDriver,
    VendorInsurance,
)

_SEVERITY_RANK = {s: i for i, s in enumerate(INSURANCE_SEVERITY)}
_STATUS_FILTERS = [("active", "Active"), ("inactive", "Archived"), ("all", "All")]


@login_required
def vendor_list(request: HttpRequest) -> HttpResponse:
    """Directory. Active vendors split into a needs-attention strip (lapsed / expiring /
    missing coverage) and a healthy roster; inactive vendors are archived — hidden unless
    the status filter asks for them."""
    vendors = Vendor.objects.prefetch_related(
        "vehicle_types",
        Prefetch(
            "policies",
            queryset=VendorInsurance.objects.only("id", "vendor_id", "expiry_date"),
        ),
    )

    query = request.GET.get("q", "").strip()
    if query:
        lookup = (
            Q(name__icontains=query) | Q(contact_name__icontains=query) | Q(email__icontains=query)
        )
        digits = re.sub(r"\D", "", query)
        if len(digits) >= 3:
            lookup |= Q(phone__icontains=digits)
        vendors = vendors.filter(lookup)

    status_filter = request.GET.get("status", "active").strip() or "active"
    if status_filter not in {"active", "inactive", "all"}:
        status_filter = "active"

    rows = list(vendors)
    for v in rows:
        v.summary = v.insurance_summary()
    active = [v for v in rows if v.status == Vendor.Status.ACTIVE]
    inactive = sorted(
        (v for v in rows if v.status == Vendor.Status.INACTIVE), key=lambda v: v.name.lower()
    )

    def attn_key(v: Vendor):
        days = v.summary["days"]
        return (
            _SEVERITY_RANK.get(v.summary["status"], len(INSURANCE_SEVERITY)),
            days if days is not None else 10**6,
            v.name.lower(),
        )

    attention = sorted((v for v in active if v.needs_attention), key=attn_key)
    healthy = sorted((v for v in active if not v.needs_attention), key=lambda v: v.name.lower())

    if status_filter == "inactive":
        attention, roster, archived_count = [], inactive, 0
    elif status_filter == "all":
        roster, archived_count = healthy + inactive, 0
    else:  # active (default)
        roster, archived_count = healthy, len(inactive)

    return render(
        request,
        "vendors/vendor_list.html",
        {
            "nav": "vendors",
            "page_title": "Vendors",
            "attention": attention,
            "roster": roster,
            "active_count": len(active),
            "archived_count": archived_count,
            "q": query,
            "status_filter": status_filter,
            "status_options": _STATUS_FILTERS,
        },
    )


_BANNER_TONE = {
    "expired": "danger",
    "critical": "danger",
    "urgent": "orange",
    "expiring": "warn",
    "none": "warn",
}


def _insurance_banner(summary: dict) -> dict | None:
    """The one loud moment on the detail page — only when coverage is at risk."""
    tone = _BANNER_TONE.get(summary["status"])
    if tone is None:  # valid → no banner
        return None
    if summary["status"] == "none":
        message = "No insurance on file. Add a policy to clear this vendor for assignments."
    elif summary["status"] == "expired":
        message = (
            f"Coverage {summary['label'].lower()}. Renew before assigning trips to this vendor."
        )
    else:
        message = (
            f"Coverage {summary['label'].lower()}. Renew before assigning new trips to this vendor."
        )
    return {"tone": tone, "message": message}


@login_required
def vendor_detail(request: HttpRequest, pk: int) -> HttpResponse:
    vendor = get_object_or_404(
        Vendor.objects.prefetch_related(
            "vehicle_types",
            "drivers",
            "policies",
            Prefetch(
                "documents",
                queryset=VendorDocument.objects.select_related("uploaded_by"),
            ),
        ),
        pk=pk,
    )
    vendor.summary = vendor.insurance_summary()
    vendor.banner = _insurance_banner(vendor.summary)
    return render(
        request,
        "vendors/vendor_detail.html",
        {"nav": "vendors", "page_title": vendor.name, "vendor": vendor},
    )


@login_required
def vendor_create(request: HttpRequest) -> HttpResponse:
    form = VendorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        vendor = form.save()
        messages.success(request, "Vendor added.")
        return redirect("vendor_detail", pk=vendor.pk)
    return render(
        request,
        "vendors/vendor_form.html",
        {"nav": "vendors", "page_title": "New vendor", "form": form, "target": None},
    )


@login_required
def vendor_edit(request: HttpRequest, pk: int) -> HttpResponse:
    target = get_object_or_404(Vendor, pk=pk)
    form = VendorForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Vendor updated.")
        return redirect("vendor_detail", pk=target.pk)
    return render(
        request,
        "vendors/vendor_form.html",
        {"nav": "vendors", "page_title": target.name, "form": form, "target": target},
    )


def _child_form_view(request, *, vendor, instance, form_class, title, uploader=False):
    form = form_class(request.POST or None, request.FILES or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if instance is None:
            obj.vendor = vendor
        if uploader and getattr(obj, "uploaded_by_id", None) is None:
            obj.uploaded_by = request.user
        obj.save()
        form.save_m2m()
        messages.success(request, f"{title} saved.")
        return redirect("vendor_detail", pk=vendor.pk)
    return render(
        request,
        "vendors/child_form.html",
        {"nav": "vendors", "page_title": title, "form": form, "vendor": vendor},
    )


@login_required
def driver_create(request: HttpRequest, pk: int) -> HttpResponse:
    vendor = get_object_or_404(Vendor, pk=pk)
    return _child_form_view(
        request, vendor=vendor, instance=None, form_class=VendorDriverForm, title="Add driver"
    )


@login_required
def driver_edit(request: HttpRequest, pk: int) -> HttpResponse:
    driver = get_object_or_404(VendorDriver, pk=pk)
    return _child_form_view(
        request,
        vendor=driver.vendor,
        instance=driver,
        form_class=VendorDriverForm,
        title="Edit driver",
    )


@login_required
def insurance_create(request: HttpRequest, pk: int) -> HttpResponse:
    vendor = get_object_or_404(Vendor, pk=pk)
    return _child_form_view(
        request,
        vendor=vendor,
        instance=None,
        form_class=VendorInsuranceForm,
        title="Add insurance policy",
    )


@login_required
def insurance_edit(request: HttpRequest, pk: int) -> HttpResponse:
    policy = get_object_or_404(VendorInsurance, pk=pk)
    return _child_form_view(
        request,
        vendor=policy.vendor,
        instance=policy,
        form_class=VendorInsuranceForm,
        title="Edit insurance policy",
    )


@login_required
def document_create(request: HttpRequest, pk: int) -> HttpResponse:
    vendor = get_object_or_404(Vendor, pk=pk)
    return _child_form_view(
        request,
        vendor=vendor,
        instance=None,
        form_class=VendorDocumentForm,
        title="Upload document",
        uploader=True,
    )
