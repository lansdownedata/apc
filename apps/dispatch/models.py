import re
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

    # Driver detail released to the customer at T-24h (APC-21). For a farmed-out trip we
    # don't hold the affiliate's roster, so these are free-entry; an in-house row leaves
    # them blank and `driver_info` reads through to `driver` / `vehicle`.
    driver_name = models.CharField(max_length=200, blank=True)
    driver_cell = models.CharField(max_length=32, blank=True)
    vehicle_desc = models.CharField(max_length=200, blank=True)
    vehicle_number = models.CharField(max_length=40, blank=True)
    # Stamped when the affiliate acknowledges the T-48h confirmation (APC-20).
    affiliate_confirmed_at = models.DateTimeField(null=True, blank=True)

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
    def driver_info(self) -> dict | None:
        """Unified driver / vehicle payload for the customer message (APC-21), or None
        when it isn't known yet. In-house rows derive it from the linked Driver / Vehicle;
        farmed-out rows use the free-entry fields, gated on a driver name being present.
        """
        if self.is_in_house:
            if not self.driver_id:
                return None
            v = self.vehicle
            desc = (
                " ".join(
                    str(p) for p in (getattr(v, "year", ""), v.make, v.model_name) if p
                ).strip()
                if v
                else ""
            )
            if v and v.color:
                desc = f"{desc} ({v.color})".strip()
            return {
                "name": self.driver.name,
                "cell": self.driver.phone,
                "vehicle_desc": desc,
                "vehicle_number": v.name if v else "",
            }
        if not self.driver_name:
            return None
        return {
            "name": self.driver_name,
            "cell": self.driver_cell,
            "vehicle_desc": self.vehicle_desc,
            "vehicle_number": self.vehicle_number,
        }

    @property
    def has_driver_info(self) -> bool:
        return self.driver_info is not None

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


def _split_list(raw: str) -> list[str]:
    """Comma / newline / semicolon separated free text → a clean list, order kept."""
    return [part.strip() for part in re.split(r"[,\n;]+", raw or "") if part.strip()]


class DispatchAlertConfig(models.Model):
    """Singleton — the thresholds and recipients for the dispatch exception monitor (APC-23).

    One row (pk=1). Edited on the Settings screen; read by `monitoring.run_dispatch_monitor`
    every cron tick. Defaults match the client-agreed table (2026-09-03).
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(
        default=True, help_text="Turn the whole monitor off without losing your thresholds."
    )

    # "No coverage" — hours before pickup with no confirmed driver / affiliate.
    unassigned_warn_hours = models.PositiveIntegerField(default=24)
    unassigned_critical_hours = models.PositiveIntegerField(default=4)
    # "Not en route" — minutes before pickup without an On-The-Way status.
    otw_warn_minutes = models.PositiveIntegerField(default=45)
    otw_critical_minutes = models.PositiveIntegerField(default=15)
    # "Not arrived" — minutes after pickup without an Arrived status.
    arrived_warn_minutes = models.PositiveIntegerField(default=15)
    arrived_critical_minutes = models.PositiveIntegerField(default=45)

    alert_emails = models.TextField(
        blank=True,
        help_text="Who gets the exception digest email. One per line or comma-separated. "
        "Blank falls back to the company email.",
    )
    critical_sms = models.TextField(
        blank=True,
        help_text="Phone numbers texted for critical-tier exceptions only. One per line or "
        "comma-separated. Blank means no SMS.",
    )

    class Meta:
        verbose_name = "dispatch alert configuration"

    def __str__(self) -> str:
        return "Dispatch alert configuration"

    @classmethod
    def load(cls) -> "DispatchAlertConfig":
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def email_list(self) -> list[str]:
        from django.conf import settings

        chosen = _split_list(self.alert_emails)
        return chosen or ([settings.COMPANY_EMAIL] if settings.COMPANY_EMAIL else [])

    @property
    def sms_list(self) -> list[str]:
        return _split_list(self.critical_sms)


class DispatchException(TimeStampedModel):
    """One open (or since-resolved) dispatch milestone breach on a trip (APC-23).

    At most one row per (reservation, kind) ever — a re-breach clears `resolved_at` and
    re-notifies rather than making a new row, so the board never shows the same problem
    twice. `monitoring` owns every write.
    """

    class Kind(models.TextChoices):
        UNASSIGNED = "unassigned", "No coverage"
        NOT_ON_THE_WAY = "not_otw", "Not en route"
        NOT_ARRIVED = "not_arrived", "Not arrived"

    class Tier(models.TextChoices):
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    reservation = models.ForeignKey(
        "reservations.Reservation", related_name="dispatch_exceptions", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    tier = models.CharField(max_length=12, choices=Tier.choices, default=Tier.WARNING)
    opened_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # The tier the recipients were last told about — lets an escalation warning→critical
    # re-notify without a plain re-tick doing so.
    notified_tier = models.CharField(max_length=12, blank=True, default="")

    class Meta(TimeStampedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["reservation", "kind"], name="one_exception_per_kind_per_trip"
            )
        ]

    def __str__(self) -> str:
        return f"{self.reservation_id} · {self.get_kind_display()} ({self.get_tier_display()})"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None
