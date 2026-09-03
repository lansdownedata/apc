"""Linked sets of identical trips — "56-Passenger Coach ×4" (APC-14).

A set is `Reservation.group_key`: a shared uuid and nothing else. There is no group row,
so there is nothing to keep in sync and nothing to clean up — a member is an ordinary
reservation that can be edited, deleted, priced and assigned entirely on its own. What the
key buys is the two operations a dispatcher actually asks for: *make me four of these*
(`set_group_size`) and *I changed one, change the rest* (`apply_to_group`).

Every write to `group_key` lives here.
"""

from __future__ import annotations

import uuid

from django.db import transaction

from .models import Reservation, Stop

# A wedding shuttle is the motivating case — four minibuses on one itinerary — but a
# hand-built count keeps one click from spawning hundreds of rows. Shared with the
# quote workspace's Duplicate ×N control (APC-13).
DUPLICATE_MAX = 20

# What "apply to all in group" does NOT carry, because it belongs to the one copy:
# its identity and place in the lead, its own link out to LimoAnywhere, its own
# progress through the trip, and its own revenue. Everything else on the row is
# something the reservation editor sets, so it propagates. Coverage (an affiliate offer
# or an in-house driver) is per-copy too, but it lives on `dispatch.Assignment` rather
# than here — a group of four coaches is four separate things to cover.
_PER_COPY_FIELDS = frozenset(
    {
        "id",
        "created_at",
        "updated_at",
        "lead",
        "group_key",
        "sort_order",
        "source_leg_id",
        "la_reservation_id",
        "la_confirmation",
        "trip_status",
        "revenue_status",
        "recognized_at",
        "recognized_amount",
    }
)


def propagated_fields() -> set[str]:
    """The field names "apply to all in group" copies onto the siblings.

    Derived from the model so a new editor field propagates without being listed twice;
    `test_propagated_fields_are_pinned` fails on any change, which is what forces a new
    field to be a deliberate propagate-or-not decision rather than a silent default.
    """
    return {
        f.name
        for f in Reservation._meta.concrete_fields
        if f.name not in _PER_COPY_FIELDS and not f.primary_key
    }


def _write_stops(target: Reservation, stops: list[Stop]) -> None:
    """Give `target` exactly `stops` as its route, replacing whatever it had.

    Every column of the stop travels, flight info included — a set whose airport run
    lost its airline and flight number would not be the identical trip it claims to be.
    `flight` (the aviationstack cache row) is shared rather than re-looked-up: it is
    derived from (airline, number, date, airport, direction), which are identical by
    construction, and it is disposable.

    Takes the source route as a list rather than a reservation so a caller writing it to
    N siblings reads it once instead of once per sibling.
    """
    target.stops.all().delete()
    Stop.objects.bulk_create(
        [
            Stop(
                reservation=target,
                sequence=s.sequence,
                address=s.address,
                note=s.note,
                name=s.name,
                scheduled_time=s.scheduled_time,
                latitude=s.latitude,
                longitude=s.longitude,
                airport_id=s.airport_id,
                airline_id=s.airline_id,
                flight_number=s.flight_number,
                flight_direction=s.flight_direction,
                flight_id=s.flight_id,
            )
            for s in stops
        ]
    )


def clone_reservation(
    source: Reservation,
    sort_order: int,
    *,
    group_key: uuid.UUID | None = None,
    stops: list[Stop] | None = None,
) -> Reservation:
    """One copy of `source` at `sort_order` — no LA link, revenue reset, route carried.

    Used both for an independent duplicate (`group_key=None`, APC-13) and for a member
    of a linked set (APC-14). The copy starts life unsynced and unrecognised because it
    is a new trip that nobody has booked or earned yet, however identical it looks.

    `stops` lets a caller cloning the same source repeatedly read its route once.
    """
    if stops is None:
        stops = list(source.stops.order_by("sequence"))
    clone = Reservation.objects.get(pk=source.pk)
    clone.pk = None
    clone.group_key = group_key
    clone.la_reservation_id = ""
    clone.la_confirmation = ""
    clone.trip_status = ""
    clone.revenue_status = Reservation.RevenueStatus.DEFERRED
    clone.recognized_at = None
    clone.recognized_amount = 0
    clone.sort_order = sort_order
    clone.save()
    _write_stops(clone, stops)
    clone.refresh_pickup_timezone()
    return clone


def _members(group_key: uuid.UUID):
    return Reservation.objects.filter(group_key=group_key).order_by("sort_order", "id")


@transaction.atomic
def set_group_size(reservation: Reservation, count: int) -> list[Reservation]:
    """Make the set `reservation` belongs to exactly `count` trips, and return it.

    Grows by cloning `reservation`; shrinks by deleting the highest `sort_order` members
    — never `reservation` itself, which is the copy the agent has open. A count of 1
    dissolves the set rather than leaving a group of one: the key comes off the survivor,
    so nothing renders a ×1 badge.

    New members are appended after the lead's last trip rather than squeezed in beside
    the anchor. Membership is the key, not adjacency, so every surface groups by
    `group_key`; renumbering the whole lead to keep a set contiguous would be a much
    larger write for no gain.
    """
    count = max(1, min(int(count), DUPLICATE_MAX))
    key = reservation.group_key

    if key is None:
        if count == 1:
            return [reservation]
        key = uuid.uuid4()
        reservation.group_key = key
        reservation.save(update_fields=["group_key"])

    current = list(_members(key))
    if count > len(current):
        last = reservation.lead.reservations.order_by("-sort_order").first()
        next_order = (last.sort_order + 1) if last else 0
        route = list(reservation.stops.order_by("sequence"))
        for offset in range(count - len(current)):
            current.append(
                clone_reservation(reservation, next_order + offset, group_key=key, stops=route)
            )
    elif count < len(current):
        doomed = [r for r in reversed(current) if r.pk != reservation.pk][: len(current) - count]
        _delete_members(doomed)
        current = [r for r in current if r.pk not in {d.pk for d in doomed}]

    if count == 1:
        Reservation.objects.filter(group_key=key).update(group_key=None)
        reservation.group_key = None

    return current


def _delete_members(doomed: list[Reservation]) -> None:
    """Remove trips from a shrinking set, releasing coverage on the way out.

    Same door as `reservation_delete`: an affiliate holding an offer for a trip that no
    longer exists is unreachable from any screen, so the offer is withdrawn before the
    row goes.
    """
    if not doomed:
        return
    from apps.dispatch import services as dispatch_services

    dispatch_services.release_trips(doomed, note="Trip removed from group")
    Reservation.objects.filter(pk__in=[r.pk for r in doomed]).delete()


@transaction.atomic
def apply_to_group(reservation: Reservation) -> int:
    """Copy `reservation`'s editor fields and route onto its siblings. Returns how many.

    The agent has already saved the copy they had open; this fans that state out. An
    ungrouped reservation has no siblings, so it is a no-op — callers can hand any
    reservation over without checking first.
    """
    if reservation.group_key is None:
        return 0
    fields = sorted(propagated_fields())
    values = {name: getattr(reservation, name) for name in fields}
    route = list(reservation.stops.order_by("sequence"))
    siblings = list(_members(reservation.group_key).exclude(pk=reservation.pk))
    for sibling in siblings:
        for name, value in values.items():
            setattr(sibling, name, value)
        sibling.save(update_fields=fields)
        _write_stops(sibling, route)
    return len(siblings)
