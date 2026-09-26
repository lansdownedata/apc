"""The affiliate compliance report (APC-73) — every active affiliate, worst coverage first.

Reads what already exists: each vendor's insurance policies (their status ladder lives on
`VendorInsurance`) and its documents. Three queries for the whole report, whatever the
vendor count. Feeds the exception dashboard (APC-69) through `out_of_compliance_count`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.db.models import Prefetch

from .models import Vendor, VendorDocument, VendorInsurance

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

# PLACEHOLDER — the document kinds every affiliate should have on file. Confirm the list
# with the client; it's one tuple so that's a one-line change.
EXPECTED_DOCUMENT_KINDS = (
    VendorDocument.Kind.OPERATING_AUTHORITY,
    VendorDocument.Kind.W9,
    VendorDocument.Kind.AFFILIATE_AGREEMENT,
)


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


def _row(vendor: Vendor) -> ComplianceRow:
    summary = vendor.insurance_summary()
    have = {d.kind for d in vendor.documents.all()}
    return ComplianceRow(
        vendor=vendor,
        status=summary["status"],
        expiry=summary["expiry"],
        days=summary["days"],
        missing_labels=[k.label for k in EXPECTED_DOCUMENT_KINDS if k not in have],
    )


def report(*, status: str = "", within: int | None = None) -> list[ComplianceRow]:
    """Active affiliates, worst status then soonest expiry. `status` narrows to one rung;
    `within` keeps affiliates whose governing policy expires inside N days — lapsed ones
    included, since they're past every window."""
    policies = VendorInsurance.objects.only("id", "vendor_id", "expiry_date")
    documents = VendorDocument.objects.only("id", "vendor_id", "kind")
    vendors = Vendor.objects.filter(status=Vendor.Status.ACTIVE).prefetch_related(
        Prefetch("policies", queryset=policies), Prefetch("documents", queryset=documents)
    )
    rows = [_row(v) for v in vendors]
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
