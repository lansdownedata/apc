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


class CalendlyEvent(TimeStampedModel):
    """Inbound Calendly webhook log (invitee.created / invitee.canceled).

    Mirrors PodiumEvent: the raw payload is stored whatever happens, so the shape can
    be refined against real traffic rather than guessed at, and the key is unique so
    Calendly's retries are no-ops at the database level.

    Not ZapEvent, which looks close: that model requires a `lead` FK, and on
    invitee.created the Lead does not exist yet — it is what this event creates.
    """

    class EventType(models.TextChoices):
        INVITEE_CREATED = "invitee.created", "Invitee created"
        INVITEE_CANCELED = "invitee.canceled", "Invitee canceled"

    lead = models.ForeignKey(
        "leads.Lead",
        related_name="calendly_events",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    # "<event type>:<invitee uri>", e.g.
    # "invitee.created:https://api.calendly.com/scheduled_events/<uuid>/invitees/<uuid>".
    # The URI is stable across delivery retries; the event type is part of the key
    # because a booking and its later cancellation share one invitee URI.
    idempotency_key = models.CharField(max_length=255, unique=True)
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


class QboCompany(models.Model):
    """A QuickBooks company the portal has been connected to, under an opaque key.

    Intuit requires the company id (realmID) to be stored encrypted. Synced records only need
    to know which company they were pushed to, so they carry `key` (in their `*realm_id`
    columns) and the id itself is kept here, encrypted. Reconnecting a company seen before
    reuses its key, so its records still count as pushed.
    """

    key = models.CharField(max_length=32, unique=True)
    realm_id_enc = models.TextField()

    class Meta:
        verbose_name = "QuickBooks company"
        verbose_name_plural = "QuickBooks companies"

    def __str__(self) -> str:
        return f"QuickBooks company {self.key[:8]}"

    @property
    def realm_id(self) -> str:
        from .crypto import decrypt

        return decrypt(self.realm_id_enc)

    @classmethod
    def key_for(cls, realm_id: str) -> str:
        """The company's key, created on its first connection. Ids are encrypted with a fresh
        nonce each time, so the match is made after decrypting (a handful of rows at most)."""
        import uuid

        from cryptography.fernet import InvalidToken

        from .crypto import encrypt

        for company in cls.objects.all():
            try:
                if company.realm_id == realm_id:
                    return company.key
            except InvalidToken:
                continue
        return cls.objects.create(key=uuid.uuid4().hex, realm_id_enc=encrypt(realm_id)).key


class QboCredential(TimeStampedModel):
    """The portal's one QuickBooks Online connection (APC-41). A singleton.

    Tokens and the company id are encrypted at rest (`apps.integrations.crypto`, under
    FIELD_ENCRYPTION_KEY). Never log or render them. `company_key` is the `QboCompany` key
    that synced records are stamped with.
    Intuit rotates the refresh token: `set_tokens` is called with the newest one from every
    token response. `broken_at` is set when a refresh is refused (`invalid_grant`) — only a
    reconnect clears it.
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    realm_id_enc = models.TextField(blank=True)
    company_key = models.CharField(max_length=32, blank=True)
    access_token_enc = models.TextField(blank=True)
    refresh_token_enc = models.TextField(blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_success_at = models.DateTimeField(null=True, blank=True)
    broken_at = models.DateTimeField(null=True, blank=True)
    broken_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "QuickBooks connection"

    def __str__(self) -> str:
        return "QuickBooks connection" if self.realm_id_enc else "QuickBooks (not connected)"

    @classmethod
    def load(cls) -> "QboCredential":
        obj, _ = cls.objects.get_or_create(singleton_id=1)
        return obj

    @staticmethod
    def _decrypt(value: str) -> str:
        from .crypto import decrypt

        return decrypt(value) if value else ""

    @property
    def access_token(self) -> str:
        return self._decrypt(self.access_token_enc)

    @property
    def refresh_token(self) -> str:
        return self._decrypt(self.refresh_token_enc)

    @property
    def realm_id(self) -> str:
        return self._decrypt(self.realm_id_enc)

    @realm_id.setter
    def realm_id(self, value: str) -> None:
        """Store the company id encrypted, and the key its records are stamped with."""
        from .crypto import encrypt

        self.realm_id_enc = encrypt(value) if value else ""
        self.company_key = QboCompany.key_for(value) if value else ""

    def set_tokens(self, *, access: str, refresh: str, access_expires_at, refresh_expires_at):
        from .crypto import encrypt

        self.access_token_enc = encrypt(access) if access else ""
        self.refresh_token_enc = encrypt(refresh) if refresh else ""
        self.access_expires_at = access_expires_at
        self.refresh_expires_at = refresh_expires_at

    @property
    def is_live(self) -> bool:
        return bool(self.realm_id_enc and self.refresh_token_enc and self.broken_at is None)
