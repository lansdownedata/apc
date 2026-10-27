"""The affiliate compliance report (APC-73) — every active affiliate, worst coverage first.

Reads what already exists: each vendor's insurance policies (their status ladder lives on
`VendorInsurance`) and its documents. Three queries for the whole report, whatever the
vendor count. Feeds the exception dashboard (APC-69) through `out_of_compliance_count`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Prefetch

from .models import AffiliateDocumentType, Vendor, VendorDocument, VendorInsurance

# Worst first. "none" (no policy on file) sits right after a lapsed policy: an affiliate
# with no coverage at all is as out of compliance as one whose coverage lapsed.
STATUS_ORDER = ("expired", "none", "critical", "urgent", "expiring", "valid")
STATUS_LABELS = {
    "expired": "Expired",
    "none": "No coverage on file",
    "critical": "Critical",
    "urgent": "Urgent",
    "expiring": "Expiring",
    "valid": "Valid",
}
OUT_OF_COMPLIANCE = ("expired", "none")


@dataclass
class ComplianceRow:
    vendor: Vendor
    status: str
    expiry: date | None
    days: int | None
    missing_labels: list[str] = field(default_factory=list)

    @property
    def missing_documents(self) -> int:
        return len(self.missing_labels)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


def required_types() -> list[AffiliateDocumentType]:
    """Active required document types, in Settings order."""
    return list(AffiliateDocumentType.objects.filter(active=True, required=True))


def _row(vendor: Vendor, types: list[AffiliateDocumentType]) -> ComplianceRow:
    summary = vendor.insurance_summary()
    have = {d.document_type_id for d in vendor.documents.all()}
    return ComplianceRow(
        vendor=vendor,
        status=summary["status"],
        expiry=summary["expiry"],
        days=summary["days"],
        missing_labels=[t.name for t in types if t.pk not in have],
    )


def report(*, status: str = "", within: int | None = None) -> list[ComplianceRow]:
    """Active affiliates, worst status then soonest expiry. `status` narrows to one rung;
    `within` keeps affiliates whose governing policy expires inside N days — lapsed ones
    included, since they're past every window."""
    policies = VendorInsurance.objects.only("id", "vendor_id", "expiry_date")
    documents = VendorDocument.objects.only("id", "vendor_id", "document_type_id")
    vendors = Vendor.objects.filter(status=Vendor.Status.ACTIVE).prefetch_related(
        Prefetch("policies", queryset=policies), Prefetch("documents", queryset=documents)
    )
    types = required_types()
    rows = [_row(v, types) for v in vendors]
    if status in STATUS_ORDER:
        rows = [r for r in rows if r.status == status]
    if within is not None:
        rows = [r for r in rows if r.days is not None and r.days <= within]
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    rows.sort(
        key=lambda r: (
            rank[r.status],
            r.expiry or date.max,
            r.vendor.name.lower(),
        )
    )
    return rows


def status_counts(rows: list[ComplianceRow]) -> dict[str, int]:
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for r in rows:
        counts[r.status] += 1
    return counts


def out_of_compliance_count() -> int:
    """Active affiliates with lapsed or no coverage — the APC-69 dashboard number."""
    return sum(1 for r in report() if r.status in OUT_OF_COMPLIANCE)
