from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


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

    lead = models.ForeignKey("leads.Lead", related_name="messages", on_delete=models.CASCADE)
    direction = models.CharField(max_length=4, choices=Direction.choices)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.SMS)
    body = models.TextField(blank=True)
    podium_conversation_uid = models.CharField(max_length=64, blank=True)
    podium_message_uid = models.CharField(max_length=64, blank=True)
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

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    lead = models.ForeignKey("leads.Lead", related_name="touchpoints", on_delete=models.CASCADE)
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
