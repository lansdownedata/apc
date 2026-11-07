"""The GNet requests drawer (APC-136, spec 2026-11-07 §5): rows per tab, tab counts, fragment.

The drawer is an inbox over the farm-in envelopes. Every decision it offers posts to the
APC-135 action endpoints; it never edits the trip, which is the workspace's job.
"""

from __future__ import annotations

from datetime import UTC

from django.contrib.auth.decorators import login_required
from django.db.models import Count, F, Prefetch, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.notifications.models import Notification
from apps.reservations.models import Reservation, Stop

from . import farm_in
from .models import FarmInRequest, FarmInStatusPush

TABS = ("pending", "accepted", "closed")
_S = FarmInRequest.Status
_CLOSED = (_S.DECLINED, _S.EXPIRED, _S.CANCELED)
_COUNTRIES = {"us", "usa", "united states", "united states of america"}
_MAX_PILLS = 2

# Full class literals so Tailwind's JIT finds them in this file (apps/**/*.py is in content).
_GOLD = "chip-gold"
_INFO = "bg-sky-50 text-sky-700"
_WARN = "chip-warn"
_OK = "chip-ok"
_DANGER = "chip-danger"
_GREY = "chip-ring"
_STATUS_PILLS = {
    _S.ACCEPTED: ("Accepted", _OK),
    _S.DECLINED: ("Declined", _DANGER),
    _S.EXPIRED: ("Expired", _GREY),
    _S.CANCELED: ("Canceled", _GREY),
}


def _tab_filter(tab: str, *, dispatch_only: bool) -> Q:
    if tab == "accepted":
        return Q(status=_S.ACCEPTED)
    if tab == "closed":
        return Q(status__in=_CLOSED)
    if dispatch_only:
        return Q(status=_S.PENDING, desk=FarmInRequest.Desk.DISPATCH)
    return Q(status=_S.PENDING)


def rows(*, tab: str, dispatch_only: bool) -> list[FarmInRequest]:
    """The tab's requests, pickup soonest first and an unknown pickup before all of them.

    Each row carries what the row partial renders: `kind`, `route`, `route_title`,
    `pills`, `needs_price`, `closed_reason` and `received_iso`.
    """
    stops = Stop.objects.select_related("airport", "airline").order_by("sequence")
    qs = (
        FarmInRequest.objects.filter(_tab_filter(tab, dispatch_only=dispatch_only))
        .select_related("company", "lead__contact", "reservation__vehicle")
        .prefetch_related(Prefetch("reservation__stops", queryset=stops))
        .order_by(F("pickup_at").asc(nulls_first=True), "pk")
    )
    if tab != "pending":
        pushes = FarmInStatusPush.objects.order_by("created_at", "pk")
        qs = qs.prefetch_related(Prefetch("pushes", queryset=pushes))
    result = list(qs)
    for req in result:
        _decorate(req)
    return result


def counts() -> dict[str, int]:
    """Per-tab counts plus the pending requests at the dispatch desk, in one query."""
    return FarmInRequest.objects.aggregate(
        pending=Count("pk", filter=Q(status=_S.PENDING)),
        accepted=Count("pk", filter=Q(status=_S.ACCEPTED)),
        closed=Count("pk", filter=Q(status__in=_CLOSED)),
        dispatch=Count("pk", filter=Q(status=_S.PENDING, desk=FarmInRequest.Desk.DISPATCH)),
    )


def pending_count() -> int:
    """Requests waiting for a decision: the header icon's badge."""
    return FarmInRequest.objects.filter(status=_S.PENDING).count()


def _decorate(req: FarmInRequest) -> None:
    res = req.reservation
    stops = list(res.stops.all())
    hourly = res.trip_type == Reservation.TripType.HOURLY
    ends = stops[:1] + stops[1:][-1:]
    if hourly:
        req.kind = "hourly"
    elif any(s.airport_id for s in ends):
        req.kind = "airport"
    else:
        req.kind = "p2p"
    req.route = _route(ends, res, hourly=hourly)
    req.route_title = " → ".join(s.address for s in ends if s.address)
    req.needs_price = res.vehicle_id is None or res.line_total <= 0
    req.pills = _pills(req)[:_MAX_PILLS]
    req.closed_reason = _closed_reason(req)
    # UTC to the second: every browser's Date.parse reads it (some reject microseconds).
    req.received_iso = req.received_at.astimezone(UTC).isoformat(timespec="seconds")


def _route(ends: list[Stop], res: Reservation, *, hourly: bool) -> str:
    """`Beverly Hills → LAX, UA 123`, or `Bethesda, 4 h as directed` for an hourly trip."""
    if not ends:
        return "—"
    if hourly:
        hours = format(res.billed_hours.normalize(), "f")
        return f"{_place(ends[0])}, {hours} h as directed"
    return " → ".join(_place(s) for s in ends)


def _place(stop: Stop) -> str:
    """An airport's code (with the flight when there is one), else the address's town."""
    if stop.airport_id:
        label = stop.flight_label if stop.flight_number else ""
        return f"{stop.airport.iata}, {label}" if label else stop.airport.iata
    parts = [p.strip() for p in stop.address.split(",") if p.strip()]
    if parts and parts[-1].lower() in _COUNTRIES:
        parts.pop()
    if len(parts) >= 3:
        return parts[-2]
    return parts[0] if parts else stop.name or "—"


def _pills(req: FarmInRequest) -> list[tuple[str, str]]:
    """Status pills in the spec's order (§5.2); the caller keeps the first two."""
    if req.status != _S.PENDING:
        pills = [_STATUS_PILLS[req.status]]
        if req.status == _S.ACCEPTED and req.updated_after_accept:
            pills.append(("Updated by requester", _WARN))
        return pills
    pills = []
    if req.desk == FarmInRequest.Desk.DISPATCH:
        pills.append(("Pending dispatch", _GOLD))
    if req.company_created:
        pills.append(("New affiliate", _INFO))
    if req.gnet_vehicle_type and not req.reservation.vehicle_id:
        pills.append(("Unmapped vehicle", _WARN))
    if farm_in.ALERT_UPDATED in req.alert.split(farm_in.ALERT_SEP):
        pills.append(("Updated by requester", _WARN))
    return pills


def _closed_reason(req: FarmInRequest) -> str:
    if req.status == _S.DECLINED:
        return req.decline_reason or "Declined"
    if req.status == _S.CANCELED:
        return farm_in.ALERT_CANCELED
    if req.status == _S.EXPIRED:
        return "Expired before anyone decided"
    return ""


@login_required
@require_GET
def farm_in_drawer_fragment(request: HttpRequest) -> HttpResponse:
    """The drawer's body: tabs with counts, then the tab's rows (HTML, not JSON)."""
    tab = request.GET.get("tab", "pending")
    if tab not in TABS:
        tab = "pending"
    dispatch_only = tab == "pending" and request.GET.get("dispatch") == "1"
    return render(
        request,
        "dispatch/_farm_in_rows.html",
        {
            "rows": rows(tab=tab, dispatch_only=dispatch_only),
            "counts": counts(),
            "tab": tab,
            "dispatch_only": dispatch_only,
        },
    )


@login_required
@require_GET
def pulse(request: HttpRequest) -> JsonResponse:
    """What the shell polls: the pending count, and the arrivals past `since` (two queries)."""
    try:
        since = int(request.GET.get("since", 0))
    except ValueError:
        since = 0
    kinds = [Notification.Kind.FARM_IN_NEW, Notification.Kind.FARM_IN_COVERAGE]
    arrivals = list(
        Notification.objects.unread_for(request.user)
        .filter(kind__in=kinds, pk__gt=since)
        .select_related("farm_in_request")
        .order_by("pk")
    )
    return JsonResponse(
        {
            "pending": pending_count(),
            "latest_id": arrivals[-1].pk if arrivals else None,
            "arrivals": [
                {
                    "id": n.pk,
                    "title": n.title,
                    "detail": n.detail,
                    "dispatch": n.kind == Notification.Kind.FARM_IN_COVERAGE,
                    "lead": n.farm_in_request.lead_id,
                    "request": n.farm_in_request_id,
                }
                for n in arrivals
            ],
        }
    )
