from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.reservations.models import Reservation, Stop
from apps.vendors.models import Vendor

from . import selectors, services
from .models import Assignment

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
    """Drawer body for one trip — a trip sheet, then the offer form or the coverage it has.

    Stops come from one ordered prefetch and are handed to the template as a list:
    `Reservation.pickup`/`dropoff` build fresh querysets that bypass the cache, so the
    template must never touch them (same rule as the board selector).
    """
    trip = get_object_or_404(
        Reservation.objects.select_related("lead", "lead__contact", "vehicle").prefetch_related(
            Prefetch("stops", queryset=Stop.objects.select_related("airline").order_by("sequence"))
        ),
        pk=pk,
    )
    assignment = services.active_assignment(trip)
    return render(
        request,
        "dispatch/_assign_panel.html",
        {
            "trip": trip,
            "stops": list(trip.stops.all()),
            "assignment": assignment,
            "coverage": assignment.status if assignment else selectors.COVERAGE_UNCOVERED,
            "previewed": selectors.offer_was_previewed(assignment),
            "options": selectors.vendor_options(trip, search=request.GET.get("q", "")),
            "search": request.GET.get("q", ""),
        },
    )


def _payout(request: HttpRequest) -> Decimal:
    """Parse the posted payout, refusing anything that isn't non-negative money.

    Rounded to cents here rather than left to the database: MySQL (dev/test) rounds a third
    decimal half-even and Postgres (prod) half-up, so the two would store different money.

    Every rejection leaves as an AssignmentError — the callers only catch that one, so
    anything else is a 500.
    """
    try:
        value = Decimal((request.POST.get("payout") or "").strip())
    except (InvalidOperation, TypeError) as exc:
        raise services.AssignmentError("Enter a payout amount.") from exc
    if not value.is_finite():  # NaN/sNaN/Infinity parse fine but aren't valid money
        raise services.AssignmentError("Enter a payout amount.")
    if value < 0:
        raise services.AssignmentError("Payout cannot be negative.")
    # Judge the magnitude BEFORE rounding, and against .995 rather than 1e8. Both halves
    # matter: quantize() raises InvalidOperation (not AssignmentError → a 500) once the
    # result would pass the 28-digit context precision, so it must never see a huge number;
    # and 99999999.999 is under 1e8 until it rounds up to it, overflowing
    # MoneyField(max_digits=10, decimal_places=2) at save time. Don't "simplify" this.
    if value >= Decimal("99999999.995"):
        raise services.AssignmentError("Payout is too large.")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _vendor(request: HttpRequest) -> Vendor:
    """The posted affiliate — active only, matching what the picker offers."""
    try:
        return Vendor.objects.get(pk=request.POST.get("vendor"), status=Vendor.Status.ACTIVE)
    except (Vendor.DoesNotExist, ValueError, TypeError) as exc:
        raise services.AssignmentError("Choose an active affiliate.") from exc


def _fail(exc: Exception) -> JsonResponse:
    return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@login_required
@require_POST
def offer(request: HttpRequest, pk: int) -> JsonResponse:
    """Send the trip to an affiliate and wait for their answer."""
    trip = get_object_or_404(Reservation, pk=pk)
    try:
        assignment = services.send_offer(
            trip,
            _vendor(request),
            payout=_payout(request),
            note=(request.POST.get("note") or "").strip(),
        )
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "assignment": assignment.pk})


@login_required
@require_POST
def assign(request: HttpRequest, pk: int) -> JsonResponse:
    """Record coverage arranged out of band — straight to confirmed."""
    trip = get_object_or_404(Reservation, pk=pk)
    try:
        assignment = services.assign_direct(
            trip,
            _vendor(request),
            payout=_payout(request),
            note=(request.POST.get("note") or "").strip(),
        )
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "assignment": assignment.pk})


_RESOLVERS = {
    "confirm": services.confirm,
    "decline": services.decline,
    "withdraw": services.withdraw,
}

# Staff-marking is the fallback for vendors we can't hear back from automatically. On the
# GNet channel we do hear back, and marking is actively dangerous: `services.decline` only
# changes local state, so a declined GNet offer keeps a REAL booking live on the gateway
# while `withdraw` — the only caller of `gnet_sync.cancel_assignment` — then refuses the
# now-resolved assignment. The trip reads uncovered, the dispatcher re-offers, and a second
# real vehicle is booked with the first unreachable. Withdraw is the only safe staff exit.
_GNET_STAFF_MARKS = ("confirm", "decline")


@login_required
@require_POST
def resolve(request: HttpRequest, pk: int) -> JsonResponse:
    """Confirm, decline, or withdraw an assignment.

    Staff-marked for the trip-sheet email channel; GNet assignments accept only
    `withdraw` here and are otherwise resolved by `dispatch.gnet_callback` from the
    affiliate's own response (see `_GNET_STAFF_MARKS`).
    """
    assignment = get_object_or_404(Assignment, pk=pk)
    action = request.POST.get("action", "")
    handler = _RESOLVERS.get(action)
    if handler is None:
        return _fail(services.AssignmentError("Unknown action."))
    if action in _GNET_STAFF_MARKS and assignment.channel == Assignment.Channel.GNET:
        return _fail(
            services.AssignmentError(
                "A GNet assignment resolves from the affiliate's response, not by hand. "
                "Use Withdraw to release it on the gateway."
            )
        )
    try:
        if handler is services.confirm:
            handler(assignment)
        else:
            handler(assignment, note=(request.POST.get("note") or "").strip())
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True})
