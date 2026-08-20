from decimal import Decimal

from django.db import models
from django.utils import timezone

from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


class AssignmentQuerySet(models.QuerySet):
    def active(self):
        """Offers still in play plus confirmed coverage — a trip has at most one."""
        return self.filter(status__in=Assignment.ACTIVE_STATUSES)


class Assignment(TimeStampedModel):
    """One farm-out of a trip to an affiliate vendor.

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
    vendor = models.ForeignKey(
        "vendors.Vendor", related_name="assignments", on_delete=models.PROTECT
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFERED)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.MANUAL)
    payout = MoneyField()
    note = models.TextField(blank=True)
    offered_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    objects = AssignmentQuerySet.as_manager()

    class Meta(TimeStampedModel.Meta):
        indexes = [models.Index(fields=["reservation", "status"])]

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def margin(self) -> Decimal:
        """What we keep — customer price minus the affiliate payout."""
        return self.reservation.line_total - self.payout

    def __str__(self) -> str:
        return f"{self.vendor.name} · {self.get_status_display()}"
