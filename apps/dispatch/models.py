from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


class AssignmentQuerySet(models.QuerySet):
    def active(self):
        """Offers still in play plus confirmed coverage — a trip has at most one."""
        return self.filter(status__in=Assignment.ACTIVE_STATUSES)


class Assignment(TimeStampedModel):
    """One coverage of a trip — a farm-out to an affiliate vendor, or an in-house driver.

    History is append-only: a declined or withdrawn offer keeps its row and a
    re-offer creates a new one, so past rows drive the most-used ranking and,
    later, affiliate reliability signals.
    """

    class Status(models.TextChoices):
        OFFERED = "offered", "Offered"
        CONFIRMED = "confirmed", "Confirmed"
        DECLINED = "declined", "Declined"
        WITHDRAWN = "withdrawn", "Withdrawn"

    class Channel(models.TextChoices):
        MANUAL = "manual", "Manual"
        GNET = "gnet", "GNet"

    ACTIVE_STATUSES = (Status.OFFERED, Status.CONFIRMED)

    reservation = models.ForeignKey(
        "reservations.Reservation", related_name="assignments", on_delete=models.CASCADE
    )
    # Exactly one provider per row — an affiliate (vendor) or one of our own drivers —
    # enforced by the CHECK constraints in Meta (plain constraints, so MySQL enforces them
    # too, unlike the one-active-per-trip rule that has to live in services._claim).
    vendor = models.ForeignKey(
        "vendors.Vendor",
        related_name="assignments",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    driver = models.ForeignKey(
        "fleet.Driver", related_name="assignments", null=True, blank=True, on_delete=models.PROTECT
    )
    vehicle = models.ForeignKey(
        "fleet.Vehicle",
        related_name="assignments",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFERED)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.MANUAL)
    payout = MoneyField()
    note = models.TextField(blank=True)
    # Indexed: every inbound GNet callback correlates on this column, so without one
    # the gateway's hot path full-scans the assignment table on each delivery.
    gnet_transaction_id = models.CharField(max_length=128, blank=True, db_index=True)
    offered_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    objects = AssignmentQuerySet.as_manager()

    class Meta(TimeStampedModel.Meta):
        indexes = [models.Index(fields=["reservation", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(vendor__isnull=False, driver__isnull=True)
                    | Q(vendor__isnull=True, driver__isnull=False)
                ),
                name="assignment_one_provider",
            ),
            models.CheckConstraint(
                condition=Q(vehicle__isnull=True) | Q(driver__isnull=False),
                name="assignment_vehicle_needs_driver",
            ),
        ]

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_in_house(self) -> bool:
        """Covered by one of our own drivers rather than farmed out."""
        return self.driver_id is not None

    @property
    def provider_name(self) -> str:
        """Who is covering the trip — the display seam every template reads instead of
        `assignment.vendor.name`, which is None on an in-house row."""
        return self.driver.name if self.is_in_house else self.vendor.name

    @property
    def margin(self) -> Decimal:
        """What we keep — customer price minus the affiliate payout (the whole price on an
        in-house row, where payout is 0; the drawer doesn't show it there)."""
        return self.reservation.line_total - self.payout

    def __str__(self) -> str:
        return f"{self.provider_name} · {self.get_status_display()}"


class GnetEvent(TimeStampedModel):
    """Idempotent log of every gateway exchange — mirrors integrations.ZapEvent.

    The unique idempotency_key is what stops a duplicate send: the gateway's own contract
    warns that reusing a requesterResNo after a failure books a SECOND vehicle rather than
    retrying, so a send that already succeeded must never be repeated.
    """

    class Action(models.TextChoices):
        SEND_TRIP = "send_trip", "Send trip"
        CANCEL_TRIP = "cancel_trip", "Cancel trip"
        CALLBACK = "callback", "Callback"

    class Result(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"
        PREVIEW = "preview", "Preview (not sent)"

    assignment = models.ForeignKey(
        Assignment, related_name="gnet_events", on_delete=models.CASCADE, null=True, blank=True
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=20, choices=Result.choices, default=Result.PENDING)
    response = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=160, unique=True)

    def __str__(self) -> str:
        return f"{self.get_action_display()} · {self.get_result_display()}"
