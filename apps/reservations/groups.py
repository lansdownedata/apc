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
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

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
    # Lineage, not content: `source_leg_id` is the wedding builder's handle on a trip it
    # generated, so a copy must not answer to it — otherwise the next rebuild of the day
    # would match, update or delete a trip nobody generated. The builder stamps its own
    # members itself (`rebuild_wedding_trips`).
    clone.source_leg_id = ""
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


@transaction.atomic
def copy_to_dates(source: Reservation, dates: list[date]) -> list[Reservation]:
    """One independent copy of `source` per date in `dates` (APC-17).

    For a recurring / multi-day programme — a corporate shuttle running Sep 8/9/10 built
    once. Everything travels except the date: vehicle, pricing, passenger count, the whole
    route including flight info (`clone_reservation`). Pickup *time* is kept — the shuttle
    leaves at the same hour each day — and `dropoff_date` moves with `pickup_date` so an
    overnight trip keeps its span.

    The copies are **not linked** (`group_key=None`): they differ by the one field a
    linked set's "apply to all" would overwrite. `source`'s own date is skipped, duplicates
    are collapsed, and the whole thing is capped at `DUPLICATE_MAX`.
    """
    wanted: list[date] = []
    for d in sorted(dates):
        if d != source.pickup_date and d not in wanted:
            wanted.append(d)
    wanted = wanted[:DUPLICATE_MAX]
    if not wanted:
        return []

    span = (
        source.dropoff_date - source.pickup_date
        if source.pickup_date and source.dropoff_date
        else None
    )
    route = list(source.stops.order_by("sequence"))
    last = source.lead.reservations.order_by("-sort_order").first()
    next_order = (last.sort_order + 1) if last else 0

    made: list[Reservation] = []
    for offset, day in enumerate(wanted):
        clone = clone_reservation(source, next_order + offset, stops=route)
        clone.pickup_date = day
        fields = ["pickup_date"]
        if span is not None:
            clone.dropoff_date = day + span
            fields.append("dropoff_date")
        clone.save(update_fields=fields)
        made.append(clone)
    return made


def key_for(count: int) -> uuid.UUID | None:
    """The `group_key` a freshly built set of `count` trips shares — None for a set of
    one, because a lone trip is not a set of one.

    For a builder writing rows from scratch and already knowing how many it needs (the
    public wedding submission). Anything reconciling or editing trips that already exist
    goes through `set_group_size`, which owns growing, shrinking and dissolving.
    """
    return uuid.uuid4() if count > 1 else None


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
        # The rows we are about to hand back were loaded before that UPDATE, so their
        # in-memory key is now a lie. A caller that saves one straight back — the wedding
        # rebuild does — would write it onto the row we just cleared.
        reservation.group_key = None
        for member in current:
            member.group_key = None

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
def apply_to_group(reservation: Reservation) -> list[Reservation]:
    """Copy `reservation`'s editor fields and route onto its siblings, and return them.

    The agent has already saved the copy they had open; this fans that state out. An
    ungrouped reservation has no siblings, so it is a no-op — callers can hand any
    reservation over without checking first.

    The updated rows come back rather than a count because a sibling already pushed to
    LimoAnywhere has just gone stale there, exactly as a hand-edited one does, and the
    caller is what knows to raise that alert.
    """
    if reservation.group_key is None:
        return []
    fields = sorted(propagated_fields())
    values = {name: getattr(reservation, name) for name in fields}
    route = list(reservation.stops.order_by("sequence"))
    siblings = list(_members(reservation.group_key).exclude(pk=reservation.pk))
    for sibling in siblings:
        for name, value in values.items():
            setattr(sibling, name, value)
        sibling.save(update_fields=fields)
        _write_stops(sibling, route)
    return siblings


@transaction.atomic
def delete_group(reservation: Reservation) -> int:
    """Remove `reservation` and every trip linked to it. Returns how many rows went.

    The quote workspace shows a set as one line, so its remove control has to take the
    whole line — removing only the anchor would leave a ×4 coach set silently standing
    at ×3. An ungrouped reservation is a set of one, so this is also a safe way to
    delete any single trip.
    """
    members = (
        [reservation] if reservation.group_key is None else list(_members(reservation.group_key))
    )
    _delete_members(members)
    return len(members)


@dataclass
class ReservationLine:
    """One line of a quote: a lone trip, or a whole linked set shown as "×4".

    Presentation only — the members stay separate reservations underneath, each with its
    own price, its own coverage and its own trip status.
    """

    reservation: Reservation
    members: list[Reservation] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_group(self) -> bool:
        return self.size > 1

    @property
    def total(self) -> Decimal:
        return sum((m.line_total for m in self.members), Decimal("0"))

    @property
    def passengers(self) -> int:
        """The movement's own headcount — what the customer asked for, before it was
        split across the vehicles it takes."""
        return sum(m.passengers for m in self.members)


def as_lines(reservations) -> list[ReservationLine]:
    """Collapse an ordered run of reservations into quote lines, sets folded together.

    A set takes the place of its earliest member, so the lines stay in the order the
    agent built them however far apart a set's later copies were appended. Iterates
    whatever it is handed — hand it a prefetched queryset and it costs no query of
    its own.
    """
    lines: list[ReservationLine] = []
    by_key: dict[uuid.UUID, ReservationLine] = {}
    for res in reservations:
        line = by_key.get(res.group_key) if res.group_key is not None else None
        if line is None:
            line = ReservationLine(reservation=res)
            if res.group_key is not None:
                by_key[res.group_key] = line
            lines.append(line)
        line.members.append(res)
    return lines
