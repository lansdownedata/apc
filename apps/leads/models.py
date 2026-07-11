from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.choices import Channel
from apps.core.models import TimeStampedModel


class Vehicle(TimeStampedModel):
    """Reference list of vehicle types."""

    class Klass(models.TextChoices):
        SEDAN = "sedan", "Sedan"
        SUV = "suv", "SUV"
        VAN = "van", "Van"
        MINI_COACH = "mini_coach", "Mini Coach"
        COACH = "coach", "Motor Coach"
        LIMO = "limo", "Limousine"

    name = models.CharField(max_length=80, unique=True)
    capacity = models.PositiveIntegerField(default=1)
    klass = models.CharField(max_length=20, choices=Klass.choices, default=Klass.SEDAN)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class LeadQuerySet(models.QuerySet):
    def open(self):
        """Leads still in play (new or quoted)."""
        return self.filter(status__in=[Lead.Status.NEW, Lead.Status.QUOTED])

    def open_pipeline_value(self) -> Decimal:
        return sum((lead.quote_total for lead in self.open()), Decimal("0.00"))


class Lead(TimeStampedModel):
    """A lead = one quote/order with a sales pipeline status."""

    class Status(models.TextChoices):
        NEW = "new", "New"
        QUOTED = "quoted", "Quoted"
        BOOKED = "booked", "Booked"
        LOST = "lost", "Lost"

    contact = models.ForeignKey("contacts.Contact", related_name="leads", on_delete=models.PROTECT)
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="assigned_leads",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.WEBSITE)
    notes = models.TextField(blank=True)
    lost_reason = models.CharField(max_length=255, blank=True)
    has_alert = models.BooleanField(default=False)
    quote_sent_at = models.DateTimeField(null=True, blank=True)
    quote_viewed_at = models.DateTimeField(null=True, blank=True)
    quote_expires_at = models.DateTimeField(null=True, blank=True)

    objects = LeadQuerySet.as_manager()

    @property
    def quote_no(self) -> str:
        return f"Q-{1040 + self.pk}" if self.pk else "Q-—"

    @property
    def quote_total(self) -> Decimal:
        return sum((r.line_total for r in self.reservations.all()), Decimal("0.00"))

    @property
    def reservation_count(self) -> int:
        return self.reservations.count()

    @property
    def quote_expired(self) -> bool:
        return self.quote_expires_at is not None and self.quote_expires_at <= timezone.now()

    def __str__(self) -> str:
        return f"{self.quote_no} · {self.contact.name}"
