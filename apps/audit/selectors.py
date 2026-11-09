"""Which entries a History panel shows (APC-140, spec §6.1)."""

from __future__ import annotations

from typing import Literal

from django.db.models import Q

from .display import AUTOMATIC_PREFIXES
from .models import AuditEntry

Kind = Literal["trip", "order", "contact", "company", "vendor"]
PROFILE_KINDS = ("contact", "company", "vendor")
ROWS_PER_GROUP = 10


def _scope(kind: Kind, pk: int, people_only: bool) -> Q:
    if kind == "trip":
        scope = Q(trip_id=pk)
    elif kind == "order":
        scope = Q(order_id=pk)
    else:
        scope = Q(profile=f"{kind}:{pk}")
    if people_only:
        automatic = Q(source="system")
        for prefix in AUTOMATIC_PREFIXES:
            automatic |= Q(source__startswith=prefix)
        scope &= ~automatic
    return scope


def _runs(rows: list[AuditEntry]) -> list[list[AuditEntry]]:
    """Consecutive rows sharing a `cid`: the groups `display.group_entries` will make."""
    runs: list[list[AuditEntry]] = []
    for row in rows:
        if runs and row.cid and runs[-1][0].cid == row.cid:
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def entries_for(
    kind: Kind,
    pk: int,
    *,
    before: int | None = None,
    limit_groups: int = 50,
    people_only: bool = False,
) -> list[AuditEntry]:
    """Newest first, up to `limit_groups` whole groups. `before` is the last entry of the
    page already shown; `people_only` leaves out jobs, webhooks, commands and the system
    (the "Hide automatic" lens, so a page of them never reads as empty)."""
    qs = AuditEntry.objects.filter(_scope(kind, pk, people_only))
    if before is not None:
        qs = qs.filter(pk__lt=before)
    cap = limit_groups * ROWS_PER_GROUP
    rows = list(qs.select_related("actor", "content_type").order_by("-timestamp", "-pk")[:cap])
    runs = _runs(rows)
    if len(runs) > limit_groups:
        runs = runs[:limit_groups]
    elif len(rows) == cap and len(runs) > 1:
        runs = runs[:-1]  # the cap may have cut the last group short; it leads the next page
    return [row for run in runs for row in run]


def has_older(kind: Kind, pk: int, before: int, *, people_only: bool = False) -> bool:
    """Whether anything is left past `before` (the "Show older" button)."""
    return AuditEntry.objects.filter(_scope(kind, pk, people_only), pk__lt=before).exists()
