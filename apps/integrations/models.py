from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel

# Refresh a little before actual expiry to avoid races on a live call.
REFRESH_BUFFER = timedelta(seconds=60)


class PodiumCredential(TimeStampedModel):
    """Stored Podium OAuth tokens for an organization/location."""

    organization_uid = models.CharField(max_length=64, blank=True)
    location_uid = models.CharField(max_length=64, blank=True)
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    scopes = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is None or self.expires_at <= timezone.now()

    @property
    def needs_refresh(self) -> bool:
        return self.expires_at is None or self.expires_at <= timezone.now() + REFRESH_BUFFER

    @classmethod
    def current(cls):
        """The active credential (most recently stored)."""
        return cls.objects.order_by("-id").first()

    def __str__(self) -> str:
        return f"Podium credential · {self.location_uid or self.organization_uid or 'unset'}"


class ZapEvent(TimeStampedModel):
    """Sync log — every Zapier / LimoAnywhere push, for traceability + retries."""

    class Action(models.TextChoices):
        CREATE_ACCOUNT = "create_account", "Find / Create Account"
        QUOTE_REQUEST = "quote_request", "Create Quote Request"
        CREATE_RESERVATION = "create_reservation", "Create Reservation"
        STATUS_WRITEBACK = "status_writeback", "Status Writeback"

    class Result(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"

    lead = models.ForeignKey("leads.Lead", related_name="zap_events", on_delete=models.CASCADE)
    action = models.CharField(max_length=30, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=20, choices=Result.choices, default=Result.PENDING)
    idempotency_key = models.CharField(max_length=120, unique=True)
    response = models.TextField(blank=True)

    @property
    def succeeded(self) -> bool:
        return self.result == self.Result.SUCCESS

    def __str__(self) -> str:
        return f"{self.get_action_display()} · {self.get_result_display()}"


class PodiumEvent(TimeStampedModel):
    """Inbound Podium webhook log (message.received / sent / failed)."""

    class EventType(models.TextChoices):
        MESSAGE_RECEIVED = "message.received", "Message received"
        MESSAGE_SENT = "message.sent", "Message sent"
        MESSAGE_FAILED = "message.failed", "Message failed"

    lead = models.ForeignKey(
        "leads.Lead",
        related_name="podium_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)

    def mark_processed(self) -> None:
        self.processed = True
        self.save(update_fields=["processed", "updated_at"])

    def __str__(self) -> str:
        return f"{self.event_type} · {'processed' if self.processed else 'pending'}"
