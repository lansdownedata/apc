"""The History drawer's fragment (APC-140, spec §6).

One view for every panel. It reads one page of entries, resolves their foreign keys in one
`in_bulk` per related model, and renders the groups in the zones of spec §6.3: a trip panel in
its trip's zone, an order panel per trip (order-level lines and day headings in the earliest
trip's zone), a profile panel in `TIME_ZONE` with the abbreviation always shown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import F, Model
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import dateformat, timezone

from apps.contacts.models import Company, Contact
from apps.leads.models import Lead
from apps.reservations.models import Reservation
from apps.vendors.models import Vendor

from .display import Group, TimeAt, fk_targets, group_entries, resolve_fks
from .models import AuditEntry
from .selectors import entries_for, has_older

ANCHORS: dict[str, type[Model]] = {
    "trip": Reservation,
    "order": Lead,
    "contact": Contact,
    "company": Company,
    "vendor": Vendor,
}


def _no_prefix(entry: AuditEntry) -> str:
    return ""


@dataclass
class Panel:
    """How one panel reads its entries: the zone its day headings follow (and every
    entry's, unless `zone_for` says otherwise), each line's trip prefix, and the date a
    trip's times are abbreviated on."""

    zone: str
    zone_for: Callable[[AuditEntry], str] | None = None
    prefix_for: Callable[[AuditEntry], str] = _no_prefix
    time_at: TimeAt | None = None


def _trip_panel(pk: int) -> Panel:
    trip = get_object_or_404(Reservation.objects.only("pickup_timezone", "pickup_date"), pk=pk)
    return Panel(
        zone=trip.pickup_timezone or settings.TIME_ZONE,
        time_at=lambda entry: trip.pickup_date,
    )


def _trip_label(trip: dict) -> str:
    """`Nov 14 · Airport transfer`; the trip type when it has no service."""
    service = trip["service_type__name"] or Reservation.TripType(trip["trip_type"]).label
    day = dateformat.format(trip["pickup_date"], "M j") if trip["pickup_date"] else ""
    return " · ".join(part for part in (day, service) if part)


def _order_panel(pk: int) -> Panel:
    get_object_or_404(Lead.objects.only("pk"), pk=pk)
    trips = {
        trip["pk"]: trip
        for trip in Reservation.objects.filter(lead_id=pk)
        .order_by(F("pickup_date").asc(nulls_last=True), "pickup_time", "pk")
        .values("pk", "pickup_timezone", "pickup_date", "trip_type", "service_type__name")
    }
    earliest = next(iter(trips.values()), None)

    def trip_of(entry: AuditEntry) -> dict | None:
        return trips.get(entry.trip_id, earliest)

    def zone_for(entry: AuditEntry) -> str:
        trip = trip_of(entry)
        return (trip and trip["pickup_timezone"]) or settings.TIME_ZONE

    def prefix_for(entry: AuditEntry) -> str:
        if entry.trip_id is None:
            return ""
        trip = trips.get(entry.trip_id)
        return _trip_label(trip) if trip else "Removed trip"

    def time_at(entry: AuditEntry) -> date | None:
        trip = trip_of(entry)
        return trip["pickup_date"] if trip else None

    return Panel(
        zone=(earliest and earliest["pickup_timezone"]) or settings.TIME_ZONE,
        zone_for=zone_for,
        prefix_for=prefix_for,
        time_at=time_at,
    )


def _panel(kind: str, pk: int) -> Panel:
    if kind == "trip":
        return _trip_panel(pk)
    if kind == "order":
        return _order_panel(pk)
    get_object_or_404(ANCHORS[kind].objects.only("pk"), pk=pk)
    return Panel(zone=settings.TIME_ZONE)


def _heading(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return dateformat.format(day, "D M j" if day.year == today.year else "D M j, Y")


def _rows(groups: list[Group], carried: date | None, today: date) -> list[tuple[str, Group]]:
    """Each group with the day heading that opens it ("" inside a day). `carried` is the
    day the page before ended on, so an appended page doesn't head it twice."""
    rows, last = [], carried
    for group in groups:
        rows.append((_heading(group.day, today) if group.day != last else "", group))
        last = group.day
    return rows


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _launch_date(tz: ZoneInfo) -> date:
    """The day the trail began: its first entry's, else today."""
    first = AuditEntry.objects.order_by("timestamp", "pk").values_list("timestamp", flat=True)
    when = first.first() or timezone.now()
    return when.astimezone(tz).date()


@login_required
def history(request: HttpRequest, kind: str, pk: int) -> HttpResponse:
    """The History drawer body for one trip, order, contact, company or vendor.

    `?auto=0` hides jobs, webhooks, commands and the system; `?before=<id>&part=1` returns
    only the next page's groups (the "Show older" append), with the page after it in the
    `X-History-Next` header.
    """
    if kind not in ANCHORS:
        raise Http404
    panel = _panel(kind, pk)
    people_only = request.GET.get("auto") == "0"
    part = request.GET.get("part") == "1"

    entries = entries_for(kind, pk, before=_int(request.GET.get("before")), people_only=people_only)
    groups = group_entries(
        entries,
        zone_for=panel.zone_for or (lambda entry: panel.zone),
        fk_names=resolve_fks(fk_targets(entries)),
        prefix_for=panel.prefix_for,
        time_at=panel.time_at,
        panel_zone=panel.zone,
    )
    tz = _tz(panel.zone)
    today = timezone.now().astimezone(tz).date()

    base = request.path
    next_before = None
    if entries and has_older(kind, pk, entries[-1].pk, people_only=people_only):
        next_before = entries[-1].pk
    older_url = ""
    if next_before:
        params = {"before": next_before, "part": 1}
        if people_only:
            params["auto"] = "0"
        params["day"] = groups[-1].day.isoformat()
        older_url = f"{base}?{urlencode(params)}"

    context = {
        "kind": kind,
        "groups": groups,
        "rows": _rows(groups, _day(request.GET.get("day")) if part else None, today),
        "next_before": next_before,
        "older_url": older_url,
        "people_only": people_only,
        "all_url": base,
        "people_url": f"{base}?auto=0",
        "launch_date": None if groups or part else _launch_date(tz),
    }
    if part:
        response = render(request, "audit/_history_groups.html", context)
        response["X-History-Next"] = older_url
        return response
    return render(request, "audit/_history.html", context)
