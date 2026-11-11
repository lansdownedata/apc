"""Trip status is 11 values, and nothing outside the migrations and the History labels
still names one of the eight retired ones (spec 2026-11-10 "Tests": guard test)."""

import ast
import re
from pathlib import Path

from django.conf import settings

from apps.reservations.models import Reservation

ELEVEN = [
    "pending",
    "unassigned",
    "assigned",
    "dispatched",
    "on_the_way",
    "arrived",
    "customer_in_car",
    "done",
    "cancelled",
    "late_cancel",
    "no_show",
]

# "circling", "offered" and "affiliate_assigned" are left out of the literals on purpose:
# they are also a coverage bucket, an Assignment status and a task kind. Those three are
# caught by their enum member names instead.
_LITERALS = (
    "farm_out_unassigned",
    "offered_to_affiliate",
    "dispatched_non_la",
    "cancelled_by_affiliate",
    "covid_cancellation",
)
_MEMBERS = (
    "FARM_OUT_UNASSIGNED",
    "OFFERED_TO_AFFILIATE",
    "AFFILIATE_ASSIGNED",
    "DISPATCHED_NON_LA",
    "CIRCLING",
    "CANCELLED_BY_AFFILIATE",
    "COVID_CANCELLATION",
)
# `Assignment.Status.OFFERED` is live, so OFFERED is only caught spelled out in full; the
# other member names are unique, so any alias (`_TS.CIRCLING`, `s.COVID_CANCELLATION`) counts.
_PATTERN = re.compile(
    "|".join(
        [
            *(re.escape(v) for v in _LITERALS),
            r"\.(?:" + "|".join(_MEMBERS) + r")\b",
            r"TripStatus\.OFFERED\b",
        ]
    )
)
_SKIP_DIRS = {"migrations", "tests", "__pycache__"}
_SUFFIXES = {".py", ".html", ".txt", ".js"}


def _allowed_lines() -> set[tuple[Path, int]]:
    """The lines of the `RETIRED_STATUS_LABELS` dict, the one place the old values live on."""
    path = Path(settings.BASE_DIR) / "apps" / "reservations" / "models.py"
    for node in ast.parse(path.read_text()).body:
        targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        if any(getattr(t, "id", None) == "RETIRED_STATUS_LABELS" for t in targets):
            return {(path, n) for n in range(node.lineno, node.end_lineno + 1)}
    raise AssertionError("RETIRED_STATUS_LABELS not found in apps/reservations/models.py")


def _files():
    base = Path(settings.BASE_DIR)
    for root in (base / "apps", base / "templates"):
        for path in root.rglob("*"):
            rel = path.relative_to(base)
            if path.suffix in _SUFFIXES and not _SKIP_DIRS.intersection(rel.parts):
                yield path


def test_trip_status_has_eleven_values():
    assert [s.value for s in Reservation.TripStatus] == ELEVEN


def test_dispatched_reads_as_dispatched():
    assert Reservation.TripStatus.DISPATCHED.label == "Dispatched"


def test_no_retired_statuses():
    allowed = _allowed_lines()
    base = Path(settings.BASE_DIR)
    hits = [
        f"{path.relative_to(base)}:{n}: {line.strip()}"
        for path in _files()
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if _PATTERN.search(line) and (path, n) not in allowed
    ]
    assert not hits, "Retired trip statuses still referenced:\n" + "\n".join(hits)
