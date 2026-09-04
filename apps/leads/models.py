from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel


class ServiceType(TimeStampedModel):
    """What a trip is for — Airport Transfer, Wedding, Corporate, and so on.

    Replaced the free-text `Reservation.service`, where the same six jobs were spelled
    six different ways. Edited in Settings; the same catalog feeds the reservation editor
    and the public booking widget's occasion picker, so the two can't drift apart.

    Separate axis from `Reservation.trip_type` (transfer vs hourly), which is how a trip
    is *priced*: a wedding can be either.
    """

    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="uniq_service_type_name_ci",
                violation_error_message="A service type with that name already exists.",
            )
        ]

    def __str__(self) -> str:
        return self.name


class VehicleType(TimeStampedModel):
    """A bookable class of vehicle (the client assigns types, not individual units)."""

    name = models.CharField(max_length=80, unique=True)
    capacity = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    image = models.ImageField(upload_to="vehicle-types/", blank=True)
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    # rate card (snapshotted onto each reservation at save)
    rate = MoneyField(blank=True)  # per-hour rate for this vehicle (both trip types)
    hourly_min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0, blank=True)
    transfer_min_hours = models.DecimalField(max_digits=5, decimal_places=2, default=1, blank=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


# Customer-facing quote/order reference: "APC-100068" for lead 68. Six digits from a
# 100000 base so the number reads as a real reference and transfers cleanly into
# LimoAnywhere / GNet, which is what the old four-digit "Q-1108" form did badly.
# Derived from the pk, never stored — change the base and every reference moves with it.
# The Leads-list search rebuilds this same expression in SQL (see lead_list); keep them
# in step or searching by quote number silently stops matching.
QUOTE_PREFIX = "APC"
QUOTE_NUMBER_BASE = 100_000


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
    # The raw public-form payload this lead arrived on, kept verbatim so the customer's
    # own signed resume link can rehydrate the form they filled in (spec 2026-08-30 §7.4).
    # Deliberately generic rather than a set of wedding columns: it is an inbound-request
    # archive, and `notes` stays the human-readable version an agent may freely edit.
    intake_payload = models.JSONField(default=dict, blank=True)
    # reserved — promo engine is a later feature
    promo_code = models.CharField(max_length=40, blank=True)
    quote_view_count = models.PositiveIntegerField(default=0)
    quote_last_viewed_at = models.DateTimeField(null=True, blank=True)
    # Returned by the customer on the T-7d wedding message (APC-18). Order-level: a wedding
    # is one order with several legs, and the day-of contact is entered once.
    wedding_name = models.CharField(max_length=200, blank=True)
    day_of_contact_name = models.CharField(max_length=200, blank=True)
    day_of_contact_phone = models.CharField(max_length=32, blank=True)

    objects = LeadQuerySet.as_manager()

    @property
    def quote_no(self) -> str:
        return f"{QUOTE_PREFIX}-{QUOTE_NUMBER_BASE + self.pk}" if self.pk else f"{QUOTE_PREFIX}-—"

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
# NEW → BOOKED is the phone-booking path (spec 2026-08-29): no quote email, no payment.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    Lead.Status.NEW: {Lead.Status.QUOTED, Lead.Status.LOST, Lead.Status.BOOKED},
    Lead.Status.QUOTED: {Lead.Status.LOST, Lead.Status.BOOKED},
    Lead.Status.LOST: {Lead.Status.NEW},
    Lead.Status.BOOKED: set(),
}
