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
        PREVIEW = "preview", "Preview (not sent)"

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

    conversation = models.ForeignKey(
        "messaging.Conversation",
        related_name="podium_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # Podium's stable per-event id (metadata.eventUid), identical across delivery retries.
    # NULL — not "" — when absent, so hand-built and replayed payloads don't all collide on
    # a single empty key. Unique because retries can overlap: in the 2026-07-31 incident
    # attempts 1 and 2 were in flight simultaneously, so an app-level check alone would
    # still have double-inserted.
    event_uid = models.CharField(  # noqa: DJ001 — see above
        max_length=64, unique=True, null=True, blank=True
    )
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)

    def mark_processed(self) -> None:
        self.processed = True
        self.save(update_fields=["processed", "updated_at"])

    def __str__(self) -> str:
        return f"{self.event_type} · {'processed' if self.processed else 'pending'}"


class LACustomer(TimeStampedModel):
    """Our Contact's customer record in LimoAnywhere (backend-proxy pattern).

    LA's Customer API acts *as* a customer, so we hold a generated password
    (encrypted at rest) to obtain per-customer tokens.
    """

    contact = models.OneToOneField(
        "contacts.Contact", related_name="la_customer", on_delete=models.CASCADE
    )
    la_customer_id = models.CharField(max_length=64)
    la_account_number = models.CharField(max_length=64, blank=True)
    email_used = models.EmailField()
    password_encrypted = models.TextField()

    @property
    def password(self) -> str:
        from . import crypto

        return crypto.decrypt(self.password_encrypted)

    def token(self) -> str:
        from . import limoanywhere

        return limoanywhere.get_token(username=self.email_used, password=self.password)

    def __str__(self) -> str:
        return f"LA customer {self.la_customer_id} · {self.email_used}"


class LAEvent(TimeStampedModel):
    """Inbound LimoAnywhere webhook log (reservation lifecycle events)."""

    la_customer = models.ForeignKey(
        LACustomer, related_name="events", null=True, blank=True, on_delete=models.SET_NULL
    )
    reservation = models.ForeignKey(
        "reservations.Reservation",
        related_name="la_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"{self.event} · res {self.reservation_id or '—'}"
