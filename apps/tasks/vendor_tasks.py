"""Vendor-level tasks (APC-71) — affiliate insurance renewal as owned work.

`VendorInsurance` already grades each policy (valid → expiring 30d → … → expired), but
only on the vendor page. This turns each policy into an `insurance_renewal` task for
Affiliate Management: scheduled at creation, opening 30 days before expiry, due the
morning it expires, and escalating through APC-52 like any other task.

- A policy gets a task only while its vendor is active and no later policy has
  superseded it — a renewal already on file needs no chasing.
- The task closes itself (system-done) once a policy with a later expiry is uploaded.
- Deactivating the vendor marks its unresolved tasks not-applicable.

`run` is called from `run-tasks` for every vendor, and `sync_vendor` from the vendor
screens (insurance add/edit, vendor edit) so a change shows at once. A fixed handful of
queries per run, however many vendors.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from apps.vendors.models import Vendor, VendorInsurance

from .definitions import KINDS
from .models import Task, TaskConfig

KIND = "insurance_renewal"


def _superseded(policies: list[VendorInsurance]) -> set[int]:
    """Policy pks some other policy on the same vendor outlasts."""
    latest: dict[int, object] = {}
    for p in policies:
        if p.vendor_id not in latest or p.expiry_date > latest[p.vendor_id]:
            latest[p.vendor_id] = p.expiry_date
    return {p.pk for p in policies if p.expiry_date < latest[p.vendor_id]}


def run(
    now: datetime | None = None, *, vendor_id: int | None = None, config: TaskConfig | None = None
) -> int:
    """Create, close and retire renewal tasks. Idempotent; returns the rows changed."""
    config = config or TaskConfig.load()
    if not config.enabled:
        return 0
    now = now or timezone.now()
    kind = KINDS[KIND]
    policies = VendorInsurance.objects.filter(vendor__status=Vendor.Status.ACTIVE)
    tasks = Task.objects.filter(kind=KIND)
    if vendor_id is not None:
        policies = policies.filter(vendor_id=vendor_id)
        tasks = tasks.filter(vendor_id=vendor_id)
    policies = list(policies)
    superseded = _superseded(policies)
    have = set(tasks.values_list("insurance_id", flat=True))
    owner = config.owner_for(kind.department)

    new = []
    for policy in policies:
        if policy.pk in have or policy.pk in superseded:
            continue
        opens_at = max(kind.opens.resolve(policy.expiry_date), now)
        new.append(
            Task(
                kind=KIND,
                vendor_id=policy.vendor_id,
                insurance=policy,
                department=kind.department,
                assignee=owner,
                status=Task.Status.SCHEDULED if opens_at > now else Task.Status.OPEN,
                opens_at=opens_at,
                due_at=kind.due.resolve(policy.expiry_date),
            )
        )
    Task.objects.bulk_create(new)

    unresolved = tasks.filter(status__in=Task.UNRESOLVED)
    changed = len(new)
    changed += unresolved.filter(insurance_id__in=superseded).update(
        status=Task.Status.DONE, completed_at=now, completed_by=None, updated_at=now
    )
    changed += unresolved.exclude(vendor__status=Vendor.Status.ACTIVE).update(
        status=Task.Status.NOT_APPLICABLE, completed_at=now, completed_by=None, updated_at=now
    )
    return changed


def sync_vendor(vendor: Vendor) -> int:
    """The hook for the vendor screens — the same pass, for one vendor."""
    return run(vendor_id=vendor.pk)
