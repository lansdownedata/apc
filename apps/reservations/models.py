from decimal import Decimal

from django.conf import settings
from django.db import models

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
    service = models.CharField(max_length=120, blank=True)
    pickup_date = models.DateField(null=True, blank=True)
    pickup_time = models.TimeField(null=True, blank=True)
    passengers = models.PositiveIntegerField(default=1)

    # transfer pricing
    base_rate = MoneyField()
    # hourly pricing
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    hourly_rate = MoneyField()
    min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    la_reservation_id = models.CharField(max_length=64, blank=True)
    la_confirmation = models.CharField("LA confirmation #", max_length=64, blank=True)
    trip_status = models.CharField(
        max_length=32, choices=TripStatus.choices, blank=True, default=""
    )
    sort_order = models.PositiveIntegerField(default=0)

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

    # --- pricing ---
    @property
    def billed_hours(self) -> Decimal:
        return max(Decimal(self.hours or 0), Decimal(self.min_hours or 0))

    @property
    def min_applied(self) -> bool:
        return self.trip_type == self.TripType.HOURLY and Decimal(self.hours or 0) < Decimal(
            self.min_hours or 0
        )

    @property
    def line_total(self) -> Decimal:
        if self.trip_type == self.TripType.HOURLY:
            amount = self.billed_hours * Decimal(self.hourly_rate or 0)
        else:
            amount = Decimal(self.base_rate or 0)
        return amount.quantize(Decimal("0.01"))

    # --- routing ---
    @property
    def ordered_stops(self):
        return self.stops.order_by("sequence")

    @property
    def pickup(self):
        return self.ordered_stops.first()

    @property
    def dropoff(self):
        return self.ordered_stops.last()

    @property
    def is_multi_stop(self) -> bool:
        return self.stops.count() > 2

    # --- dispatch status ---
    @property
    def trip_phase(self) -> str:
        return TRIP_PHASE_BY_STATUS.get(self.trip_status, "")

    @property
    def is_cancelled(self) -> bool:
        return self.trip_phase == "Cancelled"

    def __str__(self) -> str:
        return f"{self.get_trip_type_display()} · {self.service or 'Reservation'}"


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

    class Meta:
        ordering = ["sequence"]

    def __str__(self) -> str:
        return self.name or self.address or f"Stop {self.sequence}"


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
