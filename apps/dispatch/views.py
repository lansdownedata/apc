from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.reservations.models import Reservation

from . import selectors, services

_FILTERS = ("uncovered", "offered", "confirmed")


def _requested_day(request: HttpRequest) -> date:
    raw = (request.GET.get("day") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return timezone.localdate()


@login_required
def dispatch_board(request: HttpRequest) -> HttpResponse:
    """One day of booked trips, with what still needs coverage called out on top."""
    day = _requested_day(request)
    trips = selectors.board_trips(day)
    counts = selectors.strip_counts(trips)

    active_filter = request.GET.get("f", "")
    if active_filter in _FILTERS:
        trips = [t for t in trips if t.coverage == active_filter]
    else:
        active_filter = ""

    return render(
        request,
        "dispatch/board.html",
        {
            "trips": trips,
            "counts": counts,
            "day": day,
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "today": timezone.localdate(),
            "active_filter": active_filter,
            "nav": "dispatch",
            "page_title": "Dispatch",
        },
    )


@login_required
def assign_panel(request: HttpRequest, pk: int) -> HttpResponse:
    """Drawer body for one trip — the offer form, or the coverage it already has."""
    trip = get_object_or_404(
        Reservation.objects.select_related("lead", "lead__contact", "vehicle"), pk=pk
    )
    assignment = services.active_assignment(trip)
    return render(
        request,
        "dispatch/_assign_panel.html",
        {
            "trip": trip,
            "assignment": assignment,
            "options": selectors.vendor_options(trip, search=request.GET.get("q", "")),
            "search": request.GET.get("q", ""),
        },
    )
