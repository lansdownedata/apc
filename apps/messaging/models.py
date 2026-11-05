from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class Conversation(TimeStampedModel):
    """A customer's message thread. Exists independently of any Lead.

    One per Contact, all channels interleaved — each Message carries its own channel
    and Podium conversation uid, so the thread stays reconcilable with Podium without
    splitting the inbox into one row per channel.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ARCHIVED = "archived", "Archived"

    contact = models.OneToOneField(
        "contacts.Contact", related_name="conversation", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    last_message_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="archived_conversations",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["-last_message_at", "-id"]

    @property
    def is_archived(self) -> bool:
        return self.status == self.Status.ARCHIVED

    def __str__(self) -> str:
        return f"Conversation · {self.contact.name}"


class Message(TimeStampedModel):
    """A Podium conversation message (multi-channel). Inbound arrives via webhook."""

    class Direction(models.TextChoices):
        IN = "in", "Inbound"
        OUT = "out", "Outbound"

    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"
        FACEBOOK = "facebook", "Facebook"
        WHATSAPP = "whatsapp", "WhatsApp"
        APPLE = "apple", "Apple"
        # A planner's "Request a change" (APC-68). Arrives through our own page, not Podium,
        # so there is nothing to reply to on this channel — staff answer by SMS or email.
        PORTAL = "portal", "Planner portal"

    class DeliveryStatus(models.TextChoices):
        SENT = "sent", "Sent"
        RECEIVED = "received", "Received"
        FAILED = "failed", "Failed"

    conversation = models.ForeignKey(
        "messaging.Conversation", related_name="messages", on_delete=models.CASCADE
    )
    direction = models.CharField(max_length=4, choices=Direction.choices)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.SMS)
    body = models.TextField(blank=True)
    podium_conversation_uid = models.CharField(max_length=64, blank=True)
    podium_message_uid = models.CharField(max_length=64, blank=True)
    # Who actually sent it. Podium's webhooks identify an agent only by user UID, and
    # an agent may reply from the Podium app instead of our composer — so the name is
    # resolved and denormalised at ingest rather than derived from the lead.
    podium_sender_uid = models.CharField(max_length=64, blank=True)
    sender_name = models.CharField(max_length=120, blank=True)
    delivery_status = models.CharField(max_length=20, choices=DeliveryStatus.choices, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]  # chronological thread

    @property
    def is_inbound(self) -> bool:
        return self.direction == self.Direction.IN

    def __str__(self) -> str:
        return f"{self.get_direction_display()} · {self.body[:40]}"


class TouchPoint(TimeStampedModel):
    """A scheduled automated message sent through Podium."""

    class Kind(models.TextChoices):
        TP1_WELCOME = "tp1_welcome", "Welcome message"
        TP2_LEAD_FOLLOWUP = "tp2_lead_followup", "Lead follow-up"
        TP3_QUOTE_SENT_SMS = "tp3_quote_sent_sms", "Quote sent (SMS)"
        TP4_VIEWED_SMS = "tp4_viewed_sms", "Quote viewed (SMS)"
        TP5_VIEWED_EMAIL = "tp5_viewed_email", "Quote viewed (Email)"
        TP6_QUOTE_FOLLOWUP = "tp6_quote_followup", "Quote follow-up"
        TP7_EXPIRING = "tp7_expiring", "Quote expiring"
        TP8_EXPIRED = "tp8_expired", "Quote expired"
        REVIEW_REQUEST = "review_request", "Review request"
        THANK_YOU = "thank_you", "Post-trip thank-you"
        PAYMENT_REMINDER = "payment_reminder", "Payment reminder"
        # Reservation-anchored service-date kinds (APC-18-22). These carry a `reservation`
        # FK and fire for a BOOKED lead — the inverse of the TP1-8 skip rules.
        WED_FINAL_DETAILS = "wed_final_details", "Wedding: final day-of details (T-7d)"
        TRIP_CONFIRM_CUSTOMER = "trip_confirm_customer", "Customer trip confirmation (T-72h)"
        TRIP_CONFIRM_CUSTOMER_2 = (
            "trip_confirm_customer_2",
            "Customer trip confirmation — second notice (T-48h)",
        )
        TRIP_CONFIRM_AFFILIATE = "trip_confirm_affiliate", "Affiliate trip confirmation (T-48h)"
        DRIVER_RELEASED = "driver_released", "Driver details released to customer"
        DRIVER_UPDATED = "driver_updated", "Driver details updated"
        STATUS_DISPATCHED = "status_dispatched", "Trip status: Dispatched"
        STATUS_ON_THE_WAY = "status_on_the_way", "Trip status: On The Way"
        STATUS_ARRIVED = "status_arrived", "Trip status: Arrived"
        # Lead-level, triggered: the customer's held deposit was released by the issuer
        # before APC confirmed the order (APC-26, Exception 1). Carries the way back.
        ORDER_AUTH_EXPIRED = "order_auth_expired", "Deposit hold released"
        ORDER_CONFIRMED = "order_confirmed", "Order confirmed"
        ORDER_CANCELLED = "order_cancelled", "Order cancelled"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    # Null only for a send with no order behind it: a staff invite, a planner invite or
    # password reset, a staff report (`sender.send_now`).
    lead = models.ForeignKey(
        "leads.Lead",
        related_name="touchpoints",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    # Set only by the reservation-anchored kinds (APC-18-22); null for every lead-level
    # kind (TP1-8, review, payment reminder), which behave exactly as before.
    reservation = models.ForeignKey(
        "reservations.Reservation",
        related_name="touchpoints",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    message = models.ForeignKey(
        "messaging.MessageTemplate",
        related_name="touchpoints",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    # A copy of the message's key, kept for history and reporting.
    kind = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    podium_message_uid = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=255, blank=True)
    # The driver / vehicle details a driver_released send carried (name, cell, vehicle_desc,
    # vehicle_number), so a later change can be compared against what the customer was told.
    driver_details = models.JSONField(null=True, blank=True)

    @property
    def is_due(self) -> bool:
        return (
            self.status == self.Status.SCHEDULED
            and self.scheduled_for is not None
            and self.scheduled_for <= timezone.now()
        )

    def __str__(self) -> str:
        name = self.message.name if self.message_id else self.get_kind_display()
        return f"{name} · {self.get_status_display()}"


class NotificationConfig(models.Model):
    """Singleton — the master switch for automated messaging.

    One row (pk=1). Each message's own on/off lives on its `MessageTemplate` row; the sender
    checks both (`rules.check`).
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(
        default=True, help_text="Master switch — turn every lifecycle message off at once."
    )

    class Meta:
        verbose_name = "notification configuration"

    def __str__(self) -> str:
        return "Notification configuration"

    @classmethod
    def load(cls) -> "NotificationConfig":
        return cls.objects.get_or_create(pk=1)[0]


class Review(TimeStampedModel):
    """A Podium review invitation + its resulting attribution."""

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    lead = models.ForeignKey("leads.Lead", related_name="reviews", on_delete=models.CASCADE)
    contact = models.ForeignKey(
        "contacts.Contact",
        related_name="reviews",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    podium_review_invite_uid = models.CharField(max_length=64, blank=True)
    delivery_status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    link_clicked = models.BooleanField(default=False)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    body = models.TextField(blank=True)
    review_site = models.CharField(max_length=80, blank=True)
    requested_at = models.DateTimeField(null=True, blank=True)

    @property
    def has_rating(self) -> bool:
        return self.rating is not None

    def __str__(self) -> str:
        return f"Review invite · {self.get_delivery_status_display()}"


class MessageTemplateQuerySet(models.QuerySet):
    def live(self):
        """Rows that are not archived."""
        return self.filter(archived_at__isnull=True)


class MessageTemplate(TimeStampedModel):
    """One automated message: its wording, trigger, timing, channels and recipients."""

    class Section(models.TextChoices):
        LEADS_QUOTES = "leads_quotes", "Leads & quotes"
        BOOKING_PAYMENTS = "booking_payments", "Booking & payments"
        TRIP_DAY = "trip_day", "Trip day"
        AFTER_TRIP = "after_trip", "After the trip"
        AFFILIATES = "affiliates", "Affiliates"
        PLANNERS = "planners", "Planners"
        STAFF_REPORTS = "staff_reports", "Staff reports"

    class Audience(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        AFFILIATE = "affiliate", "Affiliate"
        PLANNER = "planner", "Planner"
        STAFF = "staff", "Staff"

    class Direction(models.TextChoices):
        BEFORE = "before", "Before"
        AFTER = "after", "After"

    key = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    section = models.CharField(max_length=20, choices=Section.choices)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.CUSTOMER)
    trigger = models.CharField(max_length=40)
    timing_amount = models.PositiveIntegerField(default=0)
    timing_unit = models.CharField(max_length=10, default="minutes")
    timing_direction = models.CharField(
        max_length=6, choices=Direction.choices, default=Direction.AFTER
    )
    send_sms = models.BooleanField(default=True)
    send_email = models.BooleanField(default=True)
    subject = models.CharField(max_length=200, blank=True)
    email_body = models.TextField(blank=True)
    sms_body = models.TextField(blank=True)
    recipients = models.JSONField(default=list, blank=True)
    staff_emails = models.JSONField(default=list, blank=True)
    service_types = models.ManyToManyField("leads.ServiceType", blank=True, related_name="+")
    enabled = models.BooleanField(default=True)
    seeded = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = MessageTemplateQuerySet.as_manager()

    def __str__(self) -> str:
        return self.name or self.key

    @property
    def offset(self) -> timedelta:
        """The send offset from the trigger, negative for "before"."""
        span = timedelta(**{self.timing_unit or "minutes": self.timing_amount or 0})
        return -span if self.timing_direction == self.Direction.BEFORE else span

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(
            channel for channel, on in (("sms", self.send_sms), ("email", self.send_email)) if on
        )


class Delivery(TimeStampedModel):
    """One send of a TouchPoint to one recipient on one channel."""

    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    touchpoint = models.ForeignKey(TouchPoint, related_name="deliveries", on_delete=models.CASCADE)
    message = models.ForeignKey(
        MessageTemplate, related_name="deliveries", on_delete=models.PROTECT
    )
    channel = models.CharField(max_length=5, choices=Channel.choices)
    recipient_label = models.CharField(max_length=160, blank=True)
    address = models.CharField(max_length=254)
    combine_key = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    reason = models.CharField(max_length=255, blank=True)
    provider_id = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=["message", "address", "combine_key", "status"])]

    def __str__(self) -> str:
        return f"{self.channel} to {self.address} · {self.get_status_display()}"
