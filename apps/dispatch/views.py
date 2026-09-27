from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef, Prefetch
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.contacts.models import Contact
from apps.core.phone import to_e164
from apps.fleet.models import Driver, Vehicle
from apps.leads.models import Lead, VehicleType
from apps.reservations import editor as reservation_editor
from apps.reservations import services as reservation_services
from apps.reservations.models import Reservation, Stop, TripStatusEvent
from apps.tasks import selectors as task_selectors
from apps.tasks.selectors import attach_green_lit
from apps.vendors.models import Vendor, VendorDriver

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
    today = timezone.localdate()
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
            # The board spans every customer, so the editor is handed no lead and no
            # trips — it fetches the one the drawer asks for. Spread first so the board's
            # own `vehicle_options` (its filter, ordered by name) stays the one in play;
            # both are the active vehicle list, so the editor's picker is happy with it.
            **reservation_editor.editor_context(request, None, []),
            "vehicle_options": list(
                VehicleType.objects.filter(active=True).order_by("name").values_list("id", "name")
            ),
            "customer_options": customer_options,
            "nav": "dispatch",
            "page_title": "Dispatch",
            "columns": _COLUMNS,
            # What the grid remembers between visits, and what it fills a remembered
            # range with. The browser could work these out itself — a Date is an absolute
            # instant, and formatting it in a zone the server sent would be correct — but
            # the window selects trip-local `pickup_date` values in COMPANY time, which
            # `BoardFilters` already resolves right here. Sending it keeps one definition
            # of "today" instead of two that can drift. (Each trip still renders in its
            # own pickup zone — see `Reservation.pickup_timezone` / the `trip_clock` filter.)
            "board_state": {
                "view": filters.view,
                "vehicle": filters.vehicle_type_id or "",
                "customer": filters.contact_id or "",
                "f": filters.coverage,
            },
            "default_range": {
                "start": today.isoformat(),
                "end": (today + timedelta(days=1)).isoformat(),
            },
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
def coverage_controls(request: HttpRequest, pk: int) -> HttpResponse:
    """The shared coverage fragment for one trip — the drawer includes it, the editor
    loads it (APC-48).

    There used to be two of these: this endpoint answered JSON for the editor's radio
    lists while the drawer rendered its own HTML. They drifted, which is the whole reason
    for the ticket. Tom Select needs real <option> elements and the drawer was already
    server-rendered, so one fragment is now the only implementation and "identical" is
    structural rather than maintained by hand.

    Assigning still posts to `dispatch_assign_driver` / `dispatch_assign` / `dispatch_offer`
    / `dispatch_resolve`, so the rules — one active assignment, a booked lead, an active
    driver — stay in services and are not touched here.
    """
    trip = get_object_or_404(
        Reservation.objects.select_related("lead", "vehicle"),
        pk=pk,
    )
    return render(
        request,
        "dispatch/_coverage_controls.html",
        coverage_context(trip, search=request.GET.get("q", "")),
    )


def coverage_context(trip: Reservation, *, search: str = "") -> dict:
    """Everything the coverage fragment draws, for whichever surface is drawing it.

    Built once here so the drawer (which renders it inline, inside a bigger panel) and the
    editor (which fetches it) cannot diverge again.
    """
    assignment = services.active_assignment(trip)
    uncovered = assignment is None
    in_house = selectors.in_house_options(trip) if uncovered else {"drivers": [], "vehicles": []}
    # No cap: the picker searches client-side, so every affiliate has to be in it.
    vendors = selectors.vendor_options(trip, search=search, limit=None) if uncovered else []
    farmed_out = bool(assignment and not assignment.is_in_house)
    roster = selectors.vendor_driver_options(assignment.vendor) if farmed_out else []
    return {
        "trip": trip,
        "assignment": assignment,
        "coverage": assignment.status if assignment else selectors.COVERAGE_UNCOVERED,
        "previewed": selectors.offer_was_previewed(assignment),
        # `_claim` refuses anything but a booked lead, so don't offer the controls at all.
        "can_assign": trip.lead.status == Lead.Status.BOOKED,
        "driver_options": selectors.driver_rich_options(in_house),
        "vehicle_options": selectors.vehicle_rich_options(in_house),
        "vendor_options": selectors.vendor_rich_options(vendors),
        # The affiliate's own roster, for the driver-and-vehicle form on confirmed
        # coverage. Empty for in-house, which carries its driver on the assignment.
        "vendor_driver_options": roster,
        "vendor_driver_create_url": (
            reverse("dispatch_vendor_driver_create", args=[assignment.vendor_id])
            if farmed_out
            else ""
        ),
        # The assignment stores the driver's NAME, not a roster id — it predates the roster
        # being read here at all, and a name typed before the row existed still has to show.
        # Match back to the row so an already-saved driver comes up selected.
        "selected_vendor_driver": next(
            (
                option["value"]
                for option in roster
                if assignment and option["label"] == assignment.driver_name
            ),
            "",
        ),
        "search": search,
        # The toggle only appears when there is a real choice to make — with no active
        # drivers the affiliate list stands alone.
        "has_roster": bool(in_house["drivers"]),
    }


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
    attach_green_lit([trip])
    checklist = task_selectors.checklist_for_trip(trip)
    return render(
        request,
        "dispatch/_assign_panel.html",
        {
            "checklist": checklist,
            # The coverage half is the shared fragment's own context — one source, so the
            # drawer and the editor cannot drift apart again (APC-48).
            **coverage_context(trip, search=request.GET.get("q", "")),
            "stops": list(trip.stops.all()),
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
    assignment = get_object_or_404(Assignment.objects.select_related("vendor"), pk=pk)
    cell = (request.POST.get("driver_cell") or "").strip()
    if cell:
        normalized = to_e164(cell)
        if normalized is None:
            return _fail(services.AssignmentError("Enter a valid driver cell number."))
        cell = normalized
    name = (request.POST.get("driver_name") or "").strip()
    # The picker posts a VendorDriver id; `driver_name` stays accepted so anything still
    # posting free text keeps working. A roster driver's own cell fills a blank box rather
    # than overwriting one the dispatcher typed.
    picked = (request.POST.get("vendor_driver") or "").strip()
    if picked.isdigit():
        roster = VendorDriver.objects.filter(pk=picked, vendor_id=assignment.vendor_id).first()
        if roster is None:
            return _fail(services.AssignmentError("That driver is not on this affiliate's roster."))
        name = roster.name
        cell = cell or roster.phone
    try:
        services.set_driver_info(
            assignment,
            name=name,
            cell=cell,
            vehicle_desc=(request.POST.get("vehicle_desc") or "").strip(),
            vehicle_number=(request.POST.get("vehicle_number") or "").strip(),
        )
    except services.AssignmentError as exc:
        return _fail(exc)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def vendor_driver_create(request: HttpRequest, pk: int) -> JsonResponse:
    """Add a driver to an affiliate's roster, from the coverage picker's create-on-type.

    Deliberately does nothing but create the row. `set_driver_info` is what releases a
    driver's details to the customer, and typing a name into a picker is not the same act
    as saving the trip's driver — so this must never become a send. Saving still does,
    exactly once, as it always has.
    """
    vendor = get_object_or_404(Vendor, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _fail(services.AssignmentError("Enter the driver's name."))
    # Case-insensitive, because the roster is small and two spellings of one person is
    # worse than reusing the row that is already there.
    driver = vendor.drivers.filter(name__iexact=name).first()
    if driver is None:
        driver = VendorDriver.objects.create(vendor=vendor, name=name)
    elif not driver.active:
        driver.active = True
        driver.save(update_fields=["active", "updated_at"])
    return JsonResponse({"ok": True, "id": driver.pk, "name": driver.name})


@login_required
@require_POST
def cancel_notice(request: HttpRequest, pk: int) -> JsonResponse:
    """Send the affiliate the "this trip is off" note, on the dispatcher's say-so."""
    assignment = get_object_or_404(Assignment.objects.select_related("vendor"), pk=pk)
    if not services.send_cancellation(assignment):
        return JsonResponse(
            {"ok": False, "error": "No email on file for that affiliate — call them."},
            status=400,
        )
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
