from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef, Prefetch
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.contacts.models import Contact
from apps.core.phone import to_e164
from apps.fleet.models import Driver, Vehicle
from apps.leads.models import Lead, VehicleType
from apps.reservations import services as reservation_services
from apps.reservations.models import Reservation, Stop, TripStatusEvent
from apps.vendors.models import Vendor

from . import selectors, services
from .board_filters import BoardFilters
from .models import Assignment


def _filter_labels(filters: BoardFilters) -> dict:
    """The picked vehicle-type / customer as objects, for the removable filter chips."""
    return {
        "vehicle": (
            VehicleType.objects.filter(pk=filters.vehicle_type_id).first()
            if filters.vehicle_type_id
            else None
        ),
        "customer": (
            Contact.objects.filter(pk=filters.contact_id).first() if filters.contact_id else None
        ),
    }


@login_required
def dispatch_board(request: HttpRequest) -> HttpResponse:
    """Booked trips over a day, a week, or a custom range — with what still needs
    coverage called out on top, and vehicle-type / customer / linked-set filters."""
    filters = BoardFilters.from_request(request)
    trips = selectors.board_trips(filters)
    counts = selectors.strip_counts(trips)  # whole window, before the coverage filter
    exceptions = selectors.exception_tally(trips)

    if filters.coverage:
        trips = [t for t in trips if t.coverage == filters.coverage]

    day_groups = selectors.day_groups(trips) if filters.is_multi_day else None

    # Only customers who actually have a booked trip — keeps the picker relevant.
    customer_options = list(
        Contact.objects.filter(
            Exists(
                Reservation.objects.filter(
                    lead__contact=OuterRef("pk"), lead__status=Lead.Status.BOOKED
                )
            )
        )
        .order_by("name")
        .values_list("id", "name")
    )

    picked = _filter_labels(filters)
    chips = []
    if picked["vehicle"]:
        chips.append({"label": picked["vehicle"].name, "url": filters.without_url("vehicle")})
    if picked["customer"]:
        chips.append({"label": picked["customer"].name, "url": filters.without_url("customer")})
    if filters.group_key:
        chips.append({"label": "Linked program", "url": filters.without_url("group")})

    return render(
        request,
        "dispatch/board.html",
        {
            "filters": filters,
            "trips": trips,
            "day_groups": day_groups,
            "counts": counts,
            "exceptions": exceptions,
            "today": timezone.localdate(),
            "active_filter": filters.coverage,
            "chips": chips,
            "view_links": [
                {"key": k, "label": lbl, "url": filters.switch_url(k), "active": filters.view == k}
                for k, lbl in (("day", "Day"), ("week", "Week"), ("range", "Range"))
            ],
            "strip": [
                {
                    "key": k,
                    "label": lbl,
                    "count": counts[k],
                    "active": filters.coverage == k,
                    "url": filters.coverage_url(k),
                }
                for k, lbl in (
                    ("uncovered", "uncovered"),
                    ("offered", "awaiting affiliate"),
                    ("confirmed", "covered"),
                )
            ],
            "vehicle_options": list(
                VehicleType.objects.filter(active=True).order_by("name").values_list("id", "name")
            ),
            "customer_options": customer_options,
            "nav": "dispatch",
            "page_title": "Dispatch",
            "columns": _COLUMNS,
        },
    )


# (key, label, alignment, client-sortable, width-class) — the row template carries a
# matching `data-<key>` for every sortable column and the same width class per cell
# (kept in sync by hand — see templates/dispatch/_board_row.html). ROUTING and FLIGHT
# are deliberately not sortable (low value, and FLIGHT has no single orderable key).
_COLUMNS = (
    ("pu", "PU", "left", True, "w-[100px] min-w-[100px]"),
    ("conf", "CONF#", "left", True, "w-[108px] min-w-[108px]"),
    ("coverage", "COVERAGE", "left", True, "min-w-[100px]"),
    ("passenger", "PASSENGER", "left", True, "min-w-[124px]"),
    ("pax", "PAX", "right", True, "w-[44px] min-w-[44px]"),
    ("svc", "SVC", "left", True, "min-w-[68px]"),
    ("routing", "ROUTING", "left", False, "w-[380px] max-w-[380px]"),
    ("flight", "FLIGHT", "left", False, "min-w-[96px]"),
    ("veh", "VEH", "left", True, "min-w-[116px]"),
    ("driver", "DRIVER", "left", True, "min-w-[110px]"),
    ("affiliate", "AFFILIATE", "left", True, "min-w-[138px]"),
    ("total", "TOTAL", "right", True, "w-[96px] min-w-[96px]"),
)

# The only statuses the drawer's Trip status control sets — the three that drive a
# customer notification (APC-22). Everything else on Reservation.TripStatus stays
# LA-driven; this is a small, curated advance, not a full status state machine here.
_MANUAL_STATUSES = (
    Reservation.TripStatus.DISPATCHED,
    Reservation.TripStatus.ON_THE_WAY,
    Reservation.TripStatus.ARRIVED,
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
            Prefetch(
                "stops",
                queryset=Stop.objects.select_related(
                    "airline", "airport", "flight", "flight__airport", "flight__airline"
                ).order_by("sequence"),
            )
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
            "in_house": (
                selectors.in_house_options(trip)
                if assignment is None
                else {"drivers": [], "vehicles": []}
            ),
            "trip_status_options": [(s, s.label) for s in _MANUAL_STATUSES],
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


def _driver(request: HttpRequest) -> Driver:
    """The posted in-house driver — active only, matching what the picker offers."""
    try:
        return Driver.objects.get(pk=request.POST.get("driver"), status=Driver.Status.ACTIVE)
    except (Driver.DoesNotExist, ValueError, TypeError) as exc:
        raise services.AssignmentError("Choose an active driver.") from exc


def _vehicle(request: HttpRequest) -> Vehicle | None:
    """The posted unit, or None — 'No vehicle' is the drawer's default choice."""
    raw = (request.POST.get("vehicle") or "").strip()
    if not raw:
        return None
    try:
        return Vehicle.objects.get(pk=raw, status=Vehicle.Status.ACTIVE)
    except (Vehicle.DoesNotExist, ValueError, TypeError) as exc:
        raise services.AssignmentError("Choose an active vehicle, or none.") from exc


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


@login_required
@require_POST
def assign_driver(request: HttpRequest, pk: int) -> JsonResponse:
    """Cover the trip with one of our own drivers — straight to confirmed, no payout."""
    trip = get_object_or_404(Reservation, pk=pk)
    try:
        assignment = services.assign_in_house(
            trip,
            _driver(request),
            vehicle=_vehicle(request),
            note=(request.POST.get("note") or "").strip(),
        )
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True, "assignment": assignment.pk})


@login_required
@require_POST
def set_status(request: HttpRequest, pk: int) -> JsonResponse:
    """Advance a covered trip's status by hand (APC-22) — Dispatched / On The Way /
    Arrived, each of which can fire a customer notification (Settings > Customer
    notifications, off by default)."""
    trip = get_object_or_404(Reservation, pk=pk)
    status = request.POST.get("status", "")
    if status not in _MANUAL_STATUSES:
        return _fail(services.AssignmentError("Unknown status."))
    covering = services.active_assignment(trip)
    if covering is None or covering.status != Assignment.Status.CONFIRMED:
        return _fail(services.AssignmentError("Confirm coverage before setting trip status."))
    reservation_services.set_trip_status(
        trip, status, user=request.user, source=TripStatusEvent.Source.MANUAL
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def confirm_customer(request: HttpRequest, pk: int) -> JsonResponse:
    """Record the customer's acknowledgement by hand (APC-19).

    The T-72h / T-48h notices ask the customer; at T-24h an unconfirmed trip moves to the
    daily office report and gets confirmed by phone instead — this writes that down. Only
    this trip, not the customer's whole day: the dispatcher confirmed what they confirmed.
    """
    trip = get_object_or_404(Reservation, pk=pk)
    reservation_services.confirm_trip_day([trip])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def driver_info(request: HttpRequest, pk: int) -> JsonResponse:
    """Save a farmed-out trip's driver + vehicle detail (APC-21)."""
    assignment = get_object_or_404(Assignment, pk=pk)
    cell = (request.POST.get("driver_cell") or "").strip()
    if cell:
        normalized = to_e164(cell)
        if normalized is None:
            return _fail(services.AssignmentError("Enter a valid driver cell number."))
        cell = normalized
    try:
        services.set_driver_info(
            assignment,
            name=(request.POST.get("driver_name") or "").strip(),
            cell=cell,
            vehicle_desc=(request.POST.get("vehicle_desc") or "").strip(),
            vehicle_number=(request.POST.get("vehicle_number") or "").strip(),
        )
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True})


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
