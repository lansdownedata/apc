from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


class VehicleType(TimeStampedModel):
    """A bookable class of vehicle (the client assigns types, not individual units)."""

    name = models.CharField(max_length=80, unique=True)
    capacity = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    image = models.ImageField(upload_to="vehicle-types/", blank=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    # rate card (snapshotted onto each reservation at save)
    rate = MoneyField()  # per-hour rate for this vehicle (both trip types)
    hourly_min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    transfer_min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        ordering = ["sort_order", "name"]

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
    # First time the public quote page was opened (not every view — see quote_view_count).
    quote_viewed_at = models.DateTimeField(null=True, blank=True)
    quote_expires_at = models.DateTimeField(null=True, blank=True)
    billing_contact = models.ForeignKey(
        "contacts.Contact",
        related_name="billing_leads",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="Leave blank when the booking contact is also billed.",
    )
    passenger_names = models.CharField(max_length=255, blank=True)
    # reserved — promo engine is a later feature
    promo_code = models.CharField(max_length=40, blank=True)
    quote_view_count = models.PositiveIntegerField(default=0)
    quote_last_viewed_at = models.DateTimeField(null=True, blank=True)

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

    @property
    def effective_billing_contact(self) -> Contact:
        """Per-lead override, else company's billing contact, else the booking contact."""
        if self.billing_contact:
            return self.billing_contact
        company = self.contact.company
        if company and company.billing_contact:
            return company.billing_contact
        return self.contact

    def can_transition(self, to_status: str) -> bool:
        return to_status in ALLOWED_TRANSITIONS.get(self.status, set())

    @property
    def payment_chip(self) -> str:
        """Short payment-status label for pipeline/list cards (pipeline spec 2026-07-12 §1)."""
        plan = getattr(self, "payment", None)
        if plan is None:
            return ""
        deposit_paid = plan.deposit_status == plan.DepositStatus.PAID
        balance_paid = plan.balance_status == plan.BalanceStatus.PAID
        if plan.balance_status == plan.BalanceStatus.FAILED:
            return "Balance failed"
        if deposit_paid and balance_paid:
            return "Paid in full"
        if deposit_paid:
            return "Deposit paid"
        return ""

    def __str__(self) -> str:
        return f"{self.quote_no} · {self.contact.name}"


# Legal manual + system transitions (spec 2026-07-12 §0). Server-authoritative.
# NOTE: BOOKED: set() here is deliberate for the manual `can_transition` gate — the Orders
# console's cancel+refund flow (`order_cancel_refund` in apps/payments/views.py) performs
# BOOKED→LOST directly as a system path (with refund handling) and does NOT consult
# `can_transition`. Don't "fix" this table to allow it; that would let the plain
# lead-mark-lost path bypass the refund flow.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    Lead.Status.NEW: {Lead.Status.QUOTED, Lead.Status.LOST},
    Lead.Status.QUOTED: {Lead.Status.LOST, Lead.Status.BOOKED},
    Lead.Status.LOST: {Lead.Status.NEW},
    Lead.Status.BOOKED: set(),
}
