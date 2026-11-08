"""Changes auditlog can't see on its own: a trip's route and passengers, and an owner's address
(APC-140, spec §3.3, §3.4).

Stops and passengers are replaced wholesale (delete + bulk_create) and an Address is a shared
row with no owner of its own, so none of them is registered. Each writer is wrapped instead:
the set is read before and after, compared, and any difference is written as one UPDATE entry
on the owner (the trip, the contact, the vendor). Values are strings or None, the same shape
auditlog stores.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from auditlog.context import auditlog_disabled
from django.db.models import Model

from apps.addresses.models import Address
from apps.reservations.models import Passenger, Reservation, Stop

from .models import AuditEntry

ROUTE_FIELDS = (
    "address",
    "name",
    "note",
    "scheduled_time",
    "airport",
    "airline",
    "flight_number",
    "flight_direction",
)
ADDRESS_FIELDS = (*Address._USER_FIELDS, "country")

Changes = dict[str, list[str | None]]


def _stop_values(stop: Stop) -> dict[str, str | None]:
    """A stop's compared fields as text: names for the airport and airline, and the time as
    `HH:MM:SS` (rendered in the trip's zone at display time)."""
    time = stop.scheduled_time
    return {
        "address": stop.address,
        "name": stop.name,
        "note": stop.note,
        "scheduled_time": time.strftime("%H:%M:%S") if time is not None else None,
        "airport": str(stop.airport) if stop.airport_id else None,
        "airline": str(stop.airline) if stop.airline_id else None,
        "flight_number": stop.flight_number,
        "flight_direction": stop.flight_direction,
    }


def _route(reservation_id: int) -> list[dict[str, str | None]]:
    stops = (
        Stop.objects.filter(reservation_id=reservation_id)
        .select_related("airport", "airline")
        .order_by("sequence")
    )
    return [_stop_values(stop) for stop in stops]


def _passenger_label(passenger: Passenger) -> str:
    """The name, the lead marker and the mobile masked to its last four, each when present:
    "Ana Ruiz (lead) •••1234". The mobile is always shown, since messages go to it."""
    parts = [passenger.name] if passenger.name else []
    if passenger.mobile:
        parts.append(f"•••{passenger.mobile[-4:]}")
    if passenger.is_lead:
        parts.insert(1 if passenger.name else len(parts), "(lead)")
    return " ".join(parts)


def _passengers(reservation_id: int) -> str:
    rows = Passenger.objects.filter(reservation_id=reservation_id).order_by("sort", "pk")
    return ", ".join(_passenger_label(p) for p in rows)


def _snapshot(reservation_id: int, *, route: bool, passengers: bool) -> dict:
    snap = {}
    if route:
        snap["route"] = _route(reservation_id)
    if passengers:
        snap["named_passengers"] = _passengers(reservation_id)
    return snap


def _route_changes(before: list[dict], after: list[dict]) -> Changes:
    """Stop by stop, 1-based: changed fields of a kept stop, then whole stops added/removed."""
    changes: Changes = {}
    for n in range(1, max(len(before), len(after)) + 1):
        old = before[n - 1] if n <= len(before) else None
        new = after[n - 1] if n <= len(after) else None
        if old is None:
            changes[f"route.{n}"] = [None, new["address"]]
        elif new is None:
            changes[f"route.{n}"] = [old["address"], None]
        else:
            for name in ROUTE_FIELDS:
                if old[name] != new[name]:
                    changes[f"route.{n}.{name}"] = [old[name], new[name]]
    return changes


@contextmanager
def track_children(
    reservation: Reservation, *, route: bool = True, passengers: bool = False
) -> Iterator[None]:
    """Log what the block did to the trip's route and/or named passengers as one entry.

    The passenger list is keyed `named_passengers`: `passengers` is the trip's own headcount
    field, logged by auditlog on the trip's save. One SELECT per tracked set on the way in
    and again on the way out, and one INSERT when anything differs. An exception in the
    block writes nothing, and so does a block run under auditlog's `disable_auditlog()`.
    """
    if auditlog_disabled.get():
        yield
        return
    before = _snapshot(reservation.pk, route=route, passengers=passengers)
    yield
    after = _snapshot(reservation.pk, route=route, passengers=passengers)
    changes: Changes = {}
    if route:
        changes.update(_route_changes(before["route"], after["route"]))
    if passengers and before["named_passengers"] != after["named_passengers"]:
        changes["named_passengers"] = [before["named_passengers"], after["named_passengers"]]
    if changes:
        AuditEntry.objects.log_create(reservation, action=AuditEntry.Action.UPDATE, changes=changes)


def _address_values(address: Address | None) -> dict[str, str]:
    """The compared fields; with no address yet, what a new Address starts with (so a first
    address that leaves the default country alone logs no country change)."""
    if address is None:
        return {name: Address._meta.get_field(name).get_default() for name in ADDRESS_FIELDS}
    return {name: getattr(address, name) for name in ADDRESS_FIELDS}


@contextmanager
def track_address(owner: Model, *, slot: str) -> Iterator[None]:
    """Log what the block did to the address in `owner.<slot>` as one entry on the owner.

    The block may write a different Address instance than the owner holds, so the slot is
    re-read from the database afterwards. A first address reads as the model's defaults
    before. The owner's own FK change, when the slot is filled for the first time, is
    auditlog's entry on the owner's save and is not repeated here. Nothing is written under
    auditlog's `disable_auditlog()`.
    """
    if auditlog_disabled.get():
        yield
        return
    before = _address_values(getattr(owner, slot))
    yield
    fresh = type(owner).objects.select_related(slot).get(pk=owner.pk)
    after = _address_values(getattr(fresh, slot))
    changes: Changes = {
        f"{slot}.{name}": [before[name], after[name]]
        for name in ADDRESS_FIELDS
        if before[name] != after[name]
    }
    if changes:
        AuditEntry.objects.log_create(owner, action=AuditEntry.Action.UPDATE, changes=changes)
