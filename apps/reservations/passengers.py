"""Per-trip passenger list: parsing, validation and saving."""

from __future__ import annotations

import re

from django.db import transaction

from apps.audit.children import track_children
from apps.core.phone import to_e164

from .models import Passenger, Reservation

NAME_MAX = 120


def split_names(text: str) -> list[str]:
    """Split a free-text names blob on commas, semicolons and newlines."""
    return [part.strip()[:NAME_MAX] for part in re.split(r"[,;\n]+", text or "") if part.strip()]


def parse_passengers(raw: object) -> list[dict] | None:
    """Validate a posted passenger list. None (key absent) means leave the list alone."""
    from .drafts import DraftError  # drafts imports this module

    if raw is None:
        return None
    if not isinstance(raw, list):
        raise DraftError("Passengers must be a list.")
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DraftError("Passengers must be a list.")
        name = str(item.get("name") or "").strip()[:NAME_MAX]
        mobile_raw = str(item.get("mobile") or "").strip()
        if not name and not mobile_raw:
            continue
        number = len(rows) + 1
        mobile = ""
        if mobile_raw:
            mobile = to_e164(mobile_raw)
            if not mobile:
                raise DraftError(f"Passenger {number}'s mobile isn't a valid phone number.")
        rows.append(
            {
                "name": name,
                "mobile": mobile,
                "is_lead": bool(item.get("lead")) and bool(mobile),
                "sort": len(rows),
            }
        )
    if sum(1 for r in rows if r["is_lead"]) > 1:
        raise DraftError("Only one passenger can be the lead.")
    return rows


def save_passengers(reservation: Reservation, rows: list[dict]) -> list[Passenger]:
    """Replace the trip's passenger list with `rows` under a row lock."""
    from .drafts import DraftError  # drafts imports this module

    if sum(1 for r in rows if r.get("is_lead")) > 1:
        raise DraftError("Only one passenger can be the lead.")
    with transaction.atomic():
        locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
        with track_children(locked, route=False, passengers=True):
            locked.named_passengers.all().delete()
            return Passenger.objects.bulk_create(
                [
                    Passenger(
                        reservation=locked,
                        name=r["name"],
                        mobile=r.get("mobile", ""),
                        is_lead=r.get("is_lead", False),
                        sort=r.get("sort", i),
                    )
                    for i, r in enumerate(rows)
                ]
            )
