"""Vendor management — directory, detail, and CRUD."""

from __future__ import annotations

import re

from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .models import INSURANCE_SEVERITY, Vendor, VendorInsurance

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
