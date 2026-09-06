from datetime import date, datetime, time, timedelta
from decimal import ROUND_UP, Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.utils import dateformat
from django.utils import timezone as dj_timezone
from django.utils.timesince import timesince

from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel

# Trip status -> dispatch phase (mirrors the LimoAnywhere taxonomy, grouped for display).
TRIP_PHASE_BY_STATUS = {
    "unassigned": "Created",
    "farm_out_unassigned": "Created",
    "pending": "Created",
    "offered": "Offered to Driver",
    "assigned": "Driver is Assigned",
    "dispatched": "Driver is Assigned",
    "on_the_way": "En Route to Pickup",
    "circling": "Circling",
    "arrived": "Waiting at Pickup",
    "customer_in_car": "Driving Passenger",
    "done": "Completing",
    "cancelled": "Cancelled",
    "cancelled_by_affiliate": "Cancelled",
    "late_cancel": "Cancelled",
    "no_show": "Cancelled",
    "covid_cancellation": "Cancelled",
    "offered_to_affiliate": "Offered to Affiliate",
    "affiliate_assigned": "Affiliate Assigned",
    "dispatched_non_la": "Other",
}

# flightsFuture refuses dates within this many days (spec §6.1). Whether the rest of that
# window has live coverage is a separate call — see flights.LIVE_LOOKAHEAD_DAYS (as of the
# 2026-08-29 probe, only day 0 does; days 1-7 read as unavailable).
LIVE_PHASE_DAYS = 7


# One decimal place. NOT Decimal("0.10") — that has exponent -2 and quantizes to cents.
_DIME = Decimal("0.1")
_CENTS_EXP = Decimal("0.01")


def round_to_dime(amount: Decimal | None) -> Decimal:
    """Round a price up to the next dime (client, 2026-09-05).

    Up rather than nearest: rounding down would price *below* the target cost ratio. The
    most it ever adds is 9c. `ROUND_UP` is away-from-zero, which is ceiling for a price.

    Applied to the stored `rate`, never to a derived total — rounding a derived value is
    not idempotent, so it would be recomputed and re-rounded on every edit and drift.

    Re-quantized to cents so the result carries the two decimal places every other money
    value in the system does.
    """
    dimed = Decimal(amount or 0).quantize(_DIME, rounding=ROUND_UP)
    return dimed.quantize(_CENTS_EXP)


def today_at(tz_name: str) -> date:
    """Today's date in an airport's zone — a lookup at 11 PM Eastern for a Pacific airport
    must count days from the Pacific date. Blank zone → the project's local date."""
    if not tz_name:
        return dj_timezone.localdate()
    return dj_timezone.now().astimezone(ZoneInfo(tz_name)).date()


class FlightDirection(models.TextChoices):
    """Which side of a flight matters at a stop's airport: a pickup meets an *arrival*,
    a drop-off catches a *departure*; a middle stop is whichever the user says."""

    ARRIVAL = "arrival", "Arriving"
    DEPARTURE = "departure", "Departing"


class Reservation(TimeStampedModel):
    """A priced trip line item on a quote; becomes one LimoAnywhere reservation on booking."""

    class TripType(models.TextChoices):
        TRANSFER = "transfer", "Transfer"
        HOURLY = "hourly", "Hourly"

    class TripStatus(models.TextChoices):
        # Created
        UNASSIGNED = "unassigned", "Unassigned"
        FARM_OUT_UNASSIGNED = "farm_out_unassigned", "Farm-out Unassigned"
        PENDING = "pending", "Pending"
        # Offered to Driver
        OFFERED = "offered", "Offered"
        # Driver is Assigned
        ASSIGNED = "assigned", "Assigned"
        DISPATCHED = "dispatched", "Dispatched - Driver Assigned"
        # En route / circling / waiting
        ON_THE_WAY = "on_the_way", "On The Way"
        CIRCLING = "circling", "Circling"
        ARRIVED = "arrived", "Arrived"
        # Driving / completing
        CUSTOMER_IN_CAR = "customer_in_car", "Customer In Car"
        DONE = "done", "Done"
        # Cancelled
        CANCELLED = "cancelled", "Cancelled"
        CANCELLED_BY_AFFILIATE = "cancelled_by_affiliate", "Cancelled by Affiliate"
        LATE_CANCEL = "late_cancel", "Late Cancel"
        NO_SHOW = "no_show", "No Show"
        COVID_CANCELLATION = "covid_cancellation", "COVID-19 Cancellation"
        # Affiliate / other
        OFFERED_TO_AFFILIATE = "offered_to_affiliate", "Offered to Affiliate"
        AFFILIATE_ASSIGNED = "affiliate_assigned", "Affiliate is Assigned"
        DISPATCHED_NON_LA = "dispatched_non_la", "Dispatched - Driver Assigned NON LA"

    lead = models.ForeignKey("leads.Lead", related_name="reservations", on_delete=models.CASCADE)
    vehicle = models.ForeignKey(
        "leads.VehicleType", null=True, blank=True, on_delete=models.SET_NULL
    )
    trip_type = models.CharField(max_length=20, choices=TripType.choices, default=TripType.TRANSFER)
    # What the trip is for, from the Settings catalog. SET_NULL: retiring a service type
    # must never take a booked trip with it.
    service_type = models.ForeignKey(
        "leads.ServiceType", null=True, blank=True, on_delete=models.SET_NULL
    )
    pickup_date = models.DateField(null=True, blank=True)
    pickup_time = models.TimeField(null=True, blank=True)
    pickup_timezone = models.CharField(max_length=64, blank=True)
    passengers = models.PositiveIntegerField(default=1)

    # pricing: rate × billed_hours (override hours, else the rate-card minimum) + gratuity
    rate = MoneyField()  # per-hour
    # override; 0 = none → min_hours bills
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gratuity_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gratuity_flat = MoneyField()  # optional override; used when > 0
    # reserved (not wired into pricing/editor yet — see the pricing-rework spec §4c)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_flat = MoneyField()
    # Cost-based pricing (APC-26 step 3, spec 2026-09-05). Internal only — neither value
    # may ever reach the customer or the affiliate.
    #
    # `cost_ratio_pct` is the affiliate's share of the SELL PRICE, not a margin:
    #     price = affiliate_cost / (cost_ratio_pct / 100)
    # 65% on a $1,000 cost quotes $1,538.50 and earns a 35% gross margin. Read as a margin,
    # `cost / (1 - 0.65)` gives $2,857 — 86% high, and plausible enough to ship. A LOWER
    # ratio is a HIGHER price. Never label this "margin" in a field, a form or a template.
    affiliate_cost = MoneyField()  # 0 = not cost-priced
    cost_ratio_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # drop-off (enables end times + overnight trips)
    dropoff_date = models.DateField(null=True, blank=True)
    dropoff_time = models.TimeField(null=True, blank=True)

    la_reservation_id = models.CharField(max_length=64, blank=True)
    la_confirmation = models.CharField("LA confirmation #", max_length=64, blank=True)
    trip_status = models.CharField(
        max_length=32, choices=TripStatus.choices, blank=True, default=""
    )
    sort_order = models.PositiveIntegerField(default=0)
    # Stamped when the customer acknowledges the T-72h trip confirmation on the public
    # trip-sheet page (APC-19). Null until they do.
    customer_confirmed_at = models.DateTimeField(null=True, blank=True)
    # The wedding builder's leg this trip was generated from ("guests-in", "early-out", …).
    # Blank for every trip an agent added by hand, which is what keeps a rebuild from
    # touching them. Not a foreign key: legs are derived from the event, never stored.
    source_leg_id = models.CharField(max_length=40, blank=True, db_index=True)
    # Trips generated together as one order line — "56-Passenger Coach ×4" — share a key
    # (APC-14). Null is the normal state: a lone trip is not a set of one. Deliberately a
    # bare key rather than a ReservationGroup row, matching `source_leg_id`: the set has no
    # attributes of its own, only membership, and every member stays independently
    # editable, deletable and assignable. `apps.reservations.groups` owns every write.
    group_key = models.UUIDField(null=True, blank=True, db_index=True)

    class RevenueStatus(models.TextChoices):
        DEFERRED = "deferred", "Deferred"
        RECOGNIZED = "recognized", "Recognized"
        REVERSED = "reversed", "Reversed"

    revenue_status = models.CharField(
        max_length=20, choices=RevenueStatus.choices, default=RevenueStatus.DEFERRED
    )
    recognized_at = models.DateTimeField(null=True, blank=True)
    recognized_amount = MoneyField()

    class Meta:
        ordering = ["sort_order", "id"]

    @property
    def reference(self) -> str:
        """Readable per-trip id, derived from the pk (never stored) — "APC-600142".
        A distinct 600k range so it never collides with an order's "APC-100xxx"
        (leads.QUOTE_NUMBER_BASE). Change the base and every reference moves with it."""
        from apps.leads.models import QUOTE_PREFIX

        base = 600_000
        return f"{QUOTE_PREFIX}-{base + self.pk}" if self.pk else f"{QUOTE_PREFIX}-—"

    # --- pricing ---
    _CENTS = Decimal("0.01")

    @property
    def billed_hours(self) -> Decimal:
        """Override hours when the agent set any, else the vehicle's rate-card minimum.

        The override *replaces* the minimum — it may go below it (spec 2026-08-28).
        """
        override = Decimal(self.hours or 0)
        return override if override > 0 else Decimal(self.min_hours or 0)

    @property
    def min_applied(self) -> bool:
        """True when no override is set and the rate-card minimum is what's billed."""
        return Decimal(self.hours or 0) <= 0 < Decimal(self.min_hours or 0)

    @property
    def subtotal(self) -> Decimal:
        return (Decimal(self.rate or 0) * self.billed_hours).quantize(self._CENTS)

    @property
    def discount(self) -> Decimal:
        """Flat wins over percent when both are set — the `gratuity_flat` precedent."""
        flat = Decimal(self.discount_flat or 0)
        if flat > 0:
            return flat.quantize(self._CENTS)
        return (self.subtotal * Decimal(self.discount_pct or 0) / 100).quantize(self._CENTS)

    @property
    def discounted_base(self) -> Decimal:
        """The base fare the customer actually pays.

        Floored at zero: a discount bigger than the base must not produce a negative
        `line_total`, which would post a negative ledger entry.
        """
        return max(self.subtotal - self.discount, Decimal("0.00")).quantize(self._CENTS)

    @property
    def gratuity(self) -> Decimal:
        """Computed on the *discounted* base — you don't tip on money the customer
        didn't pay (spec 2026-09-05 §4.5)."""
        flat = Decimal(self.gratuity_flat or 0)
        if flat > 0:
            return flat.quantize(self._CENTS)
        return (self.discounted_base * Decimal(self.gratuity_pct or 0) / 100).quantize(self._CENTS)

    @property
    def line_total(self) -> Decimal:
        return (self.discounted_base + self.gratuity).quantize(self._CENTS)

    # --- cost-based pricing (spec 2026-09-05) ---
    @property
    def target_price(self) -> Decimal:
        """The ideal sell price for the affiliate's cost — `cost / ratio`, raw.

        Dime rounding belongs to the solver, not here, so the calculator's preview and the
        applied rate come from one code path.
        """
        cost = Decimal(self.affiliate_cost or 0)
        ratio = Decimal(self.cost_ratio_pct or 0)
        if cost <= 0 or ratio <= 0:
            return Decimal("0.00")
        return (cost / (ratio / 100)).quantize(self._CENTS)

    @property
    def quoted_profit(self) -> Decimal:
        """What we keep, on the price actually quoted.

        Reads `discounted_base`, not `target_price` — after dime rounding and any later
        hand-edit of the rate, the real base is the truth, and a discount comes out of our
        side rather than the affiliate's. Excludes gratuity, which is a pass-through.
        """
        cost = Decimal(self.affiliate_cost or 0)
        base = self.discounted_base
        # No cost, or a cost entered before a rate was applied: nothing has been quoted, so
        # report nothing. Without the `base` guard an unpriced trip reads as a full-cost
        # loss. A genuinely under-priced trip (base below cost) still returns negative,
        # which is the point.
        if cost <= 0 or base <= 0:
            return Decimal("0.00")
        return (base - cost).quantize(self._CENTS)

    @property
    def quoted_margin_pct(self) -> Decimal:
        """Gross margin — the complement of `cost_ratio_pct`, and the honest headline."""
        base = self.discounted_base
        if base <= 0 or Decimal(self.affiliate_cost or 0) <= 0:
            return Decimal("0.00")
        return (self.quoted_profit / base * 100).quantize(self._CENTS)

    # --- routing ---
    @property
    def ordered_stops(self):
        # Joined so `flight_label` and the flight pill never cost a query in a route loop.
        return self.stops.select_related(
            "airline", "airport", "flight", "flight__airport", "flight__airline"
        ).order_by("sequence")

    @property
    def pickup(self):
        return self.ordered_stops.first()

    @property
    def dropoff(self):
        return self.ordered_stops.last()

    @property
    def is_multi_stop(self) -> bool:
        return self.stops.count() > 2

    @property
    def pickup_at(self) -> datetime | None:
        """Pickup as an aware datetime in the trip's own zone, or None without a date.

        Naive `pickup_date`/`pickup_time` stay naive on the row; this is the one
        place they become an instant. Blank `pickup_timezone` uses TIME_ZONE.
        """
        if self.pickup_date is None:
            return None
        zone = self.pickup_timezone or settings.TIME_ZONE
        clock = self.pickup_time or time(0, 0)
        return datetime.combine(self.pickup_date, clock, tzinfo=ZoneInfo(zone))

    @property
    def pickup_tz_abbrev(self) -> str:
        """Zone abbreviation for display, or "" when the trip is in TIME_ZONE."""
        zone = self.pickup_timezone or ""
        if not zone or zone == settings.TIME_ZONE:
            return ""
        clock = self.pickup_time or time(12, 0)
        day = self.pickup_date or dj_timezone.localdate()
        return datetime.combine(day, clock, tzinfo=ZoneInfo(zone)).tzname() or ""

    def refresh_pickup_timezone(self) -> bool:
        """Re-resolve from the current pickup stop. No-op (no save) when unchanged."""
        from apps.reservations.timezones import resolve

        pickup = self.stops.select_related("airport").order_by("sequence").first()
        zone = resolve(pickup) if pickup is not None else ""
        if (self.pickup_timezone or "") == zone:
            return False
        self.pickup_timezone = zone
        self.save(update_fields=["pickup_timezone"])
        return True

    # --- flights ---
    _VERIFIED_STATES = frozenset({"verified", "on_time", "landed"})

    @property
    def flight_summary(self) -> dict | None:
        """The trip's one-glance flight roll-up (spec §4.4): the worst state across its
        airport stops, or None when no stop carries flight info. Iterates `stops.all()` so
        a prefetched board/card row costs nothing — never `ordered_stops` here, whose
        `.order_by()` builds a fresh queryset that ignores any prefetch."""
        states: list[str] = []
        for stop in self.stops.all():
            if not stop.airport_id or not (stop.airline_id or stop.flight_number):
                continue
            if stop.flight_id is None:
                states.append("unverified")
                continue
            state = stop.flight.pill_state
            states.append("unverified" if state == "unavailable" else state)
        if not states:
            return None
        n = len(states)
        plural = "s" if n > 1 else ""
        for bad, icon, chip, word in (
            ("cancelled", "ti-plane-off", "chip-danger", "cancelled"),
            ("delayed", "ti-clock-exclamation", "chip-warn", "delayed"),
            ("not_found", "ti-help-circle", "chip-warn", "not found"),
        ):
            count = states.count(bad)
            if count:
                many = (
                    f"{count} flights"
                    if count > 1
                    else ("1 flight" if bad == "delayed" else "Flight")
                )
                return {"state": bad, "label": f"{many} {word}", "icon": icon, "chip": chip}
        verified = sum(1 for s in states if s in self._VERIFIED_STATES)
        if verified == n:
            return {
                "state": "verified",
                "label": f"Flight{plural} verified",
                "icon": "ti-circle-check",
                "chip": "chip-ok",
            }
        if verified:
            return {
                "state": "partial",
                "label": f"{verified} of {n} verified",
                "icon": "ti-plane",
                "chip": "chip-gold",
            }
        return {
            "state": "unverified",
            "label": f"Verify flight{plural}",
            "icon": "ti-plane",
            "chip": "chip-ring",
        }

    # --- dispatch status ---
    @property
    def trip_phase(self) -> str:
        return TRIP_PHASE_BY_STATUS.get(self.trip_status, "")

    @property
    def is_cancelled(self) -> bool:
        return self.trip_phase == "Cancelled"

    @property
    def service_label(self) -> str:
        """What to call this trip in a list. Falls back to the trip type, which every
        reservation has — the catalog entry is optional and can be retired underneath one.
        """
        return self.service_type.name if self.service_type else self.get_trip_type_display()

    def __str__(self) -> str:
        return f"{self.get_trip_type_display()} · {self.service_label}"


class Stop(TimeStampedModel):
    reservation = models.ForeignKey(Reservation, related_name="stops", on_delete=models.CASCADE)
    sequence = models.PositiveIntegerField(default=0)
    address = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=160, blank=True)
    # Known limitation: scheduled_time has no date of its own — it's rendered against the
    # parent reservation's pickup_date, so an overnight charter shows every stop under the
    # same calendar date. Promote to DateTimeField if the client ever books multi-day trips.
    scheduled_time = models.TimeField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    # Flight info — meaningful only when the address was picked from the airport
    # directory (spec 2026-08-28). Airline is PROTECT: retire a carrier, never delete it.
    airport = models.ForeignKey(
        "addresses.Airport", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    airline = models.ForeignKey(
        "addresses.Airline", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    flight_number = models.CharField(max_length=6, blank=True)  # digits only
    # Which side of the flight this stop cares about (spec 2026-08-29 §4.2). First stop →
    # arrival, last → departure (forced by the draft parser); a middle stop is the user's
    # choice or blank. Blank whenever there is no airport.
    FlightDirection = FlightDirection
    flight_direction = models.CharField(
        max_length=10, choices=FlightDirection.choices, blank=True, default=""
    )
    # The cached aviationstack answer for this stop — *derived* on every save from
    # (airline, number, trip date, airport, direction), never carried in the draft
    # (flights.link_flights). SET_NULL: the cache is disposable, the stop is not.
    flight = models.ForeignKey(
        "reservations.Flight",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stops",
    )

    class Meta:
        ordering = ["sequence"]

    @property
    def flight_label(self) -> str:
        """Staff rendering, the same on every surface: "UA 123" · "United Airlines" ·
        "Flight 123" · "". For the seeded Private carrier, `flight_number` IS the tail
        number (e.g. "N561FX") — "{iata} {flight_number}" would double up the leading N
        ("N N561FX"), so just the tail number is shown (2026-08-29 §4)."""
        if self.airline_id and self.airline.is_private and self.flight_number:
            return self.flight_number
        if self.airline_id and self.flight_number:
            return f"{self.airline.iata} {self.flight_number}"
        if self.airline_id:
            return self.airline.name
        return f"Flight {self.flight_number}" if self.flight_number else ""

    @property
    def flight_label_long(self) -> str:
        """Customer rendering (quote page): the carrier's name instead of its code — except
        Private, which has no carrier name to show, only the tail number."""
        if self.airline_id and self.airline.is_private and self.flight_number:
            return self.flight_number
        if self.airline_id and self.flight_number:
            return f"{self.airline.name} {self.flight_number}"
        return self.flight_label

    @property
    def flight_pill(self) -> dict | None:
        """The linked cache row's pill (spec §7.1), or None when unverified."""
        return self.flight.pill() if self.flight_id else None

    @property
    def verify_available(self) -> bool:
        """True when this stop's airport has scheduled service and the flight isn't a
        Private tail number — gates the drawer's Verify/Refresh button (`flights.lookup`
        refuses either case, 2026-08-29 finding 2 and §3). `self.airport` / `self.airline`
        are joined by `Reservation.ordered_stops` / the dispatch prefetch, so this never
        costs its own query."""
        return bool(
            self.airport_id
            and self.airport.has_scheduled_service
            and not (self.airline_id and self.airline.is_private)
        )

    @property
    def flight_verify_payload(self) -> dict:
        """The body `POST reservations/flights/verify/` expects for this saved stop — what the
        drawer's Refresh sends. `self.reservation` is cached by the stops prefetch.

        Carries `stop: self.pk` so the endpoint can link the verified flight back to this
        row — the drawer has no other save path to do it. The editor builds its own payload
        by hand in `verifyStop` (static/js/app.js) and must never send `stop`: it verifies
        an unsaved draft that may have no Stop row at all.
        """
        res = self.reservation
        when = self.scheduled_time or res.pickup_time
        return {
            "stop": self.pk,
            "airport": self.airport_id or "",
            "airline": self.airline_id or "",
            "flight": self.flight_number,
            "date": res.pickup_date.isoformat() if res.pickup_date else "",
            "direction": self.flight_direction,
            "time": when.strftime("%H:%M") if when else "",
        }

    def __str__(self) -> str:
        return self.name or self.address or f"Stop {self.sequence}"


_PILL_CHIP = {
    "verified": "chip-ok",
    "on_time": "chip-ok",
    "landed": "chip-ok",
    "delayed": "chip-warn",
    "not_found": "chip-warn",
    "cancelled": "chip-danger",
    "unavailable": "chip-ring",
}
_PILL_ICON = {
    "delayed": "ti-clock-exclamation",
    "cancelled": "ti-plane-off",
    "not_found": "ti-help-circle",
    "unavailable": "ti-plane",
}
DELAY_THRESHOLD_MINUTES = 10  # at or under this a live flight still reads "On time"


class Flight(TimeStampedModel):
    """One aviationstack answer, cached: this airline + number on this date, as seen from
    this airport in one direction (spec 2026-08-29 §4.3). Shared by every stop on that
    flight; `checked_at` + the phase decide when a click may hit the API again.

    All datetimes are UTC and describe the flight's movement *at `airport`* — the arrival
    for `direction=arrival`, the departure otherwise. They are rendered in
    `airport.timezone`, never the viewer's.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ACTIVE = "active", "Active"
        LANDED = "landed", "Landed"
        CANCELLED = "cancelled", "Cancelled"
        DIVERTED = "diverted", "Diverted"
        INCIDENT = "incident", "Incident"
        NOT_FOUND = "not_found", "Not found"
        UNAVAILABLE = "unavailable", "Unavailable"

    class Source(models.TextChoices):
        FUTURE = "flightsFuture", "Future schedule"
        LIVE = "flights", "Live"

    airline = models.ForeignKey("addresses.Airline", on_delete=models.PROTECT, related_name="+")
    flight_number = models.CharField(max_length=6)  # digits, as on Stop
    flight_date = models.DateField()  # local date at `airport`
    airport = models.ForeignKey("addresses.Airport", on_delete=models.PROTECT, related_name="+")
    direction = models.CharField(max_length=10, choices=FlightDirection.choices)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    estimated_at = models.DateTimeField(null=True, blank=True)
    actual_at = models.DateTimeField(null=True, blank=True)
    delay_minutes = models.PositiveIntegerField(null=True, blank=True)
    terminal = models.CharField(max_length=16, blank=True)
    gate = models.CharField(max_length=16, blank=True)
    other_airport_iata = models.CharField(max_length=4, blank=True)
    other_airport_name = models.CharField(max_length=120, blank=True)
    operated_by_iata = models.CharField(max_length=3, blank=True)
    operated_by_name = models.CharField(max_length=120, blank=True)

    source = models.CharField(max_length=14, choices=Source.choices, blank=True, default="")
    checked_at = models.DateTimeField()
    raw = models.JSONField(default=dict, blank=True)

    class Meta(TimeStampedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["airline", "flight_number", "flight_date", "airport", "direction"],
                name="uniq_flight_lookup",
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.airport.iata} {self.direction} · {self.flight_date}"

    # --- phase & windows ---
    @property
    def code(self) -> str:
        return f"{self.airline.iata} {self.flight_number}"

    @property
    def is_live_phase(self) -> bool:
        return self.flight_date <= today_at(self.airport.timezone) + timedelta(days=LIVE_PHASE_DAYS)

    @property
    def recheck_window(self) -> timedelta:
        """Future & found 24 h · future & not-found / unavailable 1 h · live 5 min. Evaluated
        against the phase *now*, so a future row that ages into the live window gets the
        5-minute rule on its own — that is the whole 'moves to the live table' transition."""
        if self.is_live_phase:
            return timedelta(minutes=5)
        if self.status in (self.Status.NOT_FOUND, self.Status.UNAVAILABLE):
            return timedelta(hours=1)
        return timedelta(hours=24)

    @property
    def refresh_allowed_at(self):
        return self.checked_at + self.recheck_window

    # --- times ---
    @property
    def best_at(self):
        return self.actual_at or self.estimated_at or self.scheduled_at

    @property
    def effective_delay(self) -> int:
        """Minutes late: aviationstack's `delay` when it sent one, else estimated − scheduled.
        Never negative — early is not a state anyone dispatches around."""
        if self.delay_minutes is not None:
            return self.delay_minutes
        if self.estimated_at and self.scheduled_at:
            return max(0, int((self.estimated_at - self.scheduled_at).total_seconds() // 60))
        return 0

    def local(self, dt):
        return dt.astimezone(ZoneInfo(self.airport.timezone)) if dt else None

    def _fmt(self, dt) -> str:
        return dateformat.format(self.local(dt), "g:i A") if dt else ""

    @property
    def time_local(self) -> str:
        return self._fmt(self.best_at)

    @property
    def scheduled_local(self) -> str:
        return self._fmt(self.scheduled_at)

    @property
    def tz_abbr(self) -> str:
        return self.local(self.best_at).strftime("%Z") if self.best_at else ""

    @property
    def other_airport_label(self) -> str:
        if self.other_airport_name and self.other_airport_iata:
            return f"{self.other_airport_name} ({self.other_airport_iata})"
        return self.other_airport_iata

    # --- the pill ---
    @property
    def pill_state(self) -> str:
        s = self.status
        if s in (self.Status.CANCELLED, self.Status.DIVERTED, self.Status.INCIDENT):
            return "cancelled"
        if s == self.Status.NOT_FOUND:
            return "not_found"
        if s == self.Status.UNAVAILABLE:
            return "unavailable"
        if s == self.Status.LANDED or self.actual_at:
            return "landed"
        if self.source != self.Source.LIVE:
            return "verified"  # a schedule snapshot carries no live status
        return "delayed" if self.effective_delay > DELAY_THRESHOLD_MINUTES else "on_time"

    def pill(self) -> dict:
        """Everything a surface needs to draw this flight (spec §7.1) — the template include
        and the verify endpoint both emit exactly this, so the copy lives here once."""
        arriving = self.direction == FlightDirection.ARRIVAL
        state = self.pill_state
        code = self.code
        t, abbr, sched = self.time_local, self.tz_abbr, self.scheduled_local
        # "" when a found flight has no parseable time (scheduled/estimated/actual all
        # blank — a malformed provider time). Every branch below must drop the trailing
        # " · "/" " it would otherwise leave rather than fall through to a bare double
        # space, e.g. "UA 123 ·  " instead of just "UA 123".
        time_word = f"{t} {abbr}".strip()
        other = self.other_airport_label
        verb = "Arrives" if arriving else "Departs"
        prep = "from" if arriving else "to"
        side = f"{prep} {other}" if other else ""
        terminal = f"Terminal {self.terminal}" if self.terminal else ""
        gate = f"Gate {self.gate}" if self.gate else ""
        # timesince()'s own `now=None` default calls raw datetime.now(), not
        # django.utils.timezone.now() — pass it explicitly or frozen-time tests (and any
        # other timezone.now() patch) silently compute against real wall-clock time instead.
        # It also wraps its result in a non-breaking space (avoid_wrapping) — normalize back
        # to a plain space so "3 minutes ago" matches what the copy spec shows.
        ago = timesince(self.checked_at, dj_timezone.now()).replace("\xa0", " ")
        updated = f"updated {ago} ago"
        parts: list[str]
        if state == "verified":
            label = f"{code} · {time_word}" if time_word else code
            checked = dateformat.format(self.local(self.checked_at), "M j")
            parts = [f"{verb} {time_word}".strip(), terminal, side, f"checked {checked}"]
        elif state == "on_time":
            label = f"{code} · On time" + (f" · {time_word}" if time_word else "")
            parts = [f"{verb} {time_word}".strip(), terminal, gate, side, updated]
        elif state == "delayed":
            label = f"{code} · +{self.effective_delay}m" + (f" · {time_word}" if time_word else "")
            parts = [
                f"{verb} {time_word}".strip(),
                f"scheduled {sched}",
                terminal,
                gate,
                side,
                updated,
            ]
        elif state == "landed":
            word = "Landed" if arriving else "Departed"
            label = f"{code} · {word}" + (f" {time_word}" if time_word else "")
            parts = [terminal, gate, side, updated]
        elif state == "cancelled":
            word = "Diverted" if self.status == self.Status.DIVERTED else "Cancelled"
            label = f"{code} · {word}"
            was = f"was {'arriving' if arriving else 'departing'} {sched} {abbr}" if sched else ""
            parts = [was, side, updated]
        elif state == "not_found":
            label = f"{code} · Not found"
            when = dateformat.format(self.flight_date, "M j")
            clause = "" if self.is_live_phase else " — not found, or not published yet"
            parts = [
                f"No {code} {'arriving' if arriving else 'departing'} at {self.airport.iata} "
                f"on {when}{clause}. Check the number, or the flight may use another airport."
            ]
        else:
            label = f"{code} · Live on the day"
            parts = ["Live data available on the day of travel"]
        if self.operated_by_iata:
            parts.append(f"Operated by {self.operated_by_name or self.operated_by_iata}")
        direction_icon = "ti-plane-arrival" if arriving else "ti-plane-departure"
        return {
            "state": state,
            "chip": _PILL_CHIP[state],
            "icon": _PILL_ICON.get(state, direction_icon),
            "label": label,
            "label_compact": label.replace(f" {abbr}", "") if abbr else label,
            "detail": " · ".join(p for p in parts if p),
            "code": code,
            "direction": self.direction,
            "status": self.status,
            "source": self.source,
            "time_local": t,
            "scheduled_local": sched,
            "tz_abbr": abbr,
            "terminal": self.terminal,
            "gate": self.gate,
            "other_airport": other,
            "operated_by": self.operated_by_name or self.operated_by_iata,
            "checked_at": self.checked_at.isoformat(),
            "checked_ago": ago,
            "refresh_allowed_at": self.refresh_allowed_at.isoformat(),
        }


class TripStatusEvent(TimeStampedModel):
    """Dispatch-status history for a reservation (from LA writeback or a manual change)."""

    class Source(models.TextChoices):
        LIMOANYWHERE = "limoanywhere", "LimoAnywhere"
        MANUAL = "manual", "Manual"

    reservation = models.ForeignKey(
        Reservation, related_name="status_events", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=32, choices=Reservation.TripStatus.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.LIMOANYWHERE)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    def __str__(self) -> str:
        return f"{self.reservation_id} → {self.get_status_display()}"


# Trip statuses that count as earned revenue (the vehicle was provided), per spec §5.1.
EARNED_TERMINAL_STATUSES = (
    Reservation.TripStatus.DONE,
    Reservation.TripStatus.NO_SHOW,
)


class PricingConfig(models.Model):
    """Singleton — the default cost ratio the trip editor pre-fills (spec 2026-09-05 §3.4).

    One row (pk=1), edited on the Settings screen. Lives here rather than in `apps.settings`
    because pricing is this app's domain, matching `DispatchAlertConfig` in `apps.dispatch`
    and `NotificationConfig` in `apps.messaging` — the settings app only renders it.
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    # The affiliate's share of the sell price, NOT a margin — see `Reservation.cost_ratio_pct`.
    # The client works between 60% and 70% depending on the deal; this is the starting point,
    # and every trip can override it.
    default_cost_ratio_pct = models.DecimalField(max_digits=5, decimal_places=2, default=65)

    class Meta:
        verbose_name = "Pricing settings"
        verbose_name_plural = "Pricing settings"

    def __str__(self) -> str:
        return f"Pricing — vendor keeps {self.default_cost_ratio_pct}%"

    @classmethod
    def load(cls) -> "PricingConfig":
        return cls.objects.get_or_create(pk=1)[0]
