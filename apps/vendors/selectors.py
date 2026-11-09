"""The Vendors list's row set, shared by the list and its Excel export (APC-141)."""

import re

from django.db.models import Q, QuerySet

from .models import Vendor

STATUSES = ("active", "inactive", "all")


def clean_status(status: str | None) -> str:
    """One of STATUSES; blank or unknown is `active` (the list's default)."""
    status = (status or "").strip()
    return status if status in STATUSES else "active"


def directory(status: str, q: str) -> QuerySet[Vendor]:
    """The vendors the list shows for `status` and search `q`: `active` → ACTIVE (needs
    attention + healthy), `inactive` → INACTIVE (archived), `all` → every vendor."""
    vendors = Vendor.objects.all()
    q = (q or "").strip()
    if q:
        lookup = Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q)
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            lookup |= Q(phone__icontains=digits)
        vendors = vendors.filter(lookup)
    status = clean_status(status)
    if status == "active":
        vendors = vendors.filter(status=Vendor.Status.ACTIVE)
    elif status == "inactive":
        vendors = vendors.filter(status=Vendor.Status.INACTIVE)
    return vendors
