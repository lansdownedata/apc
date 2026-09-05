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
        STATUS_DISPATCHED = "status_dispatched", "Trip status: Dispatched"
        STATUS_ON_THE_WAY = "status_on_the_way", "Trip status: On The Way"
        STATUS_ARRIVED = "status_arrived", "Trip status: Arrived"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    lead = models.ForeignKey("leads.Lead", related_name="touchpoints", on_delete=models.CASCADE)
    # Set only by the reservation-anchored kinds (APC-18-22); null for every lead-level
    # kind (TP1-8, review, payment reminder), which behave exactly as before.
    reservation = models.ForeignKey(
        "reservations.Reservation",
        related_name="touchpoints",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    podium_message_uid = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=255, blank=True)

    @property
    def is_due(self) -> bool:
        return (
            self.status == self.Status.SCHEDULED
            and self.scheduled_for is not None
            and self.scheduled_for <= timezone.now()
        )

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.get_status_display()}"


class NotificationConfig(models.Model):
    """Singleton — per-message switches for the reservation-lifecycle messaging (APC-18-22).

    One row (pk=1). Mirrors `dispatch.DispatchAlertConfig`. Edited on the Settings screen
    (`/portal/settings/notifications/`), read by `messaging.touchpoints` at schedule and
    send time. The message *offsets* stay in code — the 7d-vs-72h question is a copy/merge
    decision (APC-27), not a slider.
    """

    # kind slug -> the `<stem>_enabled` flag on this model.
    _STEM = {
        "wed_final_details": "wedding_final_details",
        "trip_confirm_customer": "trip_confirm_customer",
        # Second wave of the same message — one switch governs both (APC-19).
        "trip_confirm_customer_2": "trip_confirm_customer",
        "trip_confirm_affiliate": "trip_confirm_affiliate",
        "driver_released": "driver_released",
        "status_dispatched": "status_dispatched",
        "status_on_the_way": "status_on_the_way",
        "status_arrived": "status_arrived",
    }

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(
        default=True, help_text="Master switch — turn every lifecycle message off at once."
    )

    wedding_final_details_enabled = models.BooleanField(
        default=True, help_text="APC-18 · T-7d wedding: request day-of contact + wedding name."
    )
    trip_confirm_customer_enabled = models.BooleanField(
        default=True,
        help_text="APC-19 · customer trip confirmation + acknowledgement (T-72h, repeated T-48h).",
    )
    trip_confirm_affiliate_enabled = models.BooleanField(
        default=True, help_text="APC-20 · T-48h affiliate trip confirmation + acknowledgement."
    )
    driver_released_enabled = models.BooleanField(
        default=True, help_text="APC-21 · release driver + vehicle details to the customer."
    )
    # APC-22 — the client may not want all three live day one, so these start opt-in.
    status_dispatched_enabled = models.BooleanField(default=False, help_text="APC-22 · Dispatched.")
    status_on_the_way_enabled = models.BooleanField(default=False, help_text="APC-22 · On The Way.")
    status_arrived_enabled = models.BooleanField(default=False, help_text="APC-22 · Arrived.")

    class Meta:
        verbose_name = "notification configuration"

    def __str__(self) -> str:
        return "Notification configuration"

    @classmethod
    def load(cls) -> "NotificationConfig":
        return cls.objects.get_or_create(pk=1)[0]

    def is_enabled(self, kind: str) -> bool:
        stem = self._STEM.get(kind)
        return bool(self.enabled and stem and getattr(self, f"{stem}_enabled", False))


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
