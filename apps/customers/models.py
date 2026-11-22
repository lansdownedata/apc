"""Customer accounts: the people who book rides, signed in at `/account/`.

A customer is NOT an `accounts.User`. Like a planner, a customer signs in through
`apps.customers.auth`, which keeps its own session keys and never sets `request.user`, so
every `@login_required` staff view sees a signed-in customer as an anonymous visitor.

Unlike `PlannerAccount`, the account keeps no copy of the email. The sign-in email is the
contact's email, read live: contact emails are unique, so one email never reaches two
accounts, staff correcting a contact's email changes how the customer signs in, and a
contact whose email is cleared cannot sign in until it has one again.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.hashers import check_password, is_password_usable, make_password
from django.db import models

from apps.core.models import TimeStampedModel


class CustomerAccount(TimeStampedModel):
    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"

    contact = models.OneToOneField(
        "contacts.Contact", related_name="customer_account", on_delete=models.CASCADE
    )
    password = models.CharField(max_length=128)
    invited_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    # True when the customer signed up from the public form rather than through an invite.
    self_registered = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_login = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # When the one-time "We found your history" banner was shown; null until then.
    history_banner_seen_at = models.DateTimeField(null=True, blank=True)
    # Per-account brake on password guessing: too many wrong passwords in a row lock
    # sign-in until `locked_until`.
    failed_logins = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta(TimeStampedModel.Meta):
        verbose_name = "customer account"

    def __str__(self) -> str:
        return self.email or f"customer account {self.pk}"

    @property
    def email(self) -> str:
        """The sign-in email: the contact's, as it is now."""
        return (self.contact.email or "").lower()

    @property
    def status(self) -> str:
        """Derived, never stored. Revoked wins over everything before it."""
        if self.revoked_at is not None:
            return self.Status.REVOKED
        if self.registered_at is not None:
            return self.Status.ACTIVE
        return self.Status.INVITED

    @property
    def can_sign_in(self) -> bool:
        return self.status == self.Status.ACTIVE and self.has_usable_password() and bool(self.email)

    # --- the bits of the user API Django's password tooling relies on ---------------

    def set_password(self, raw: str) -> None:
        self.password = make_password(raw)

    def set_unusable_password(self) -> None:
        self.password = make_password(None)

    def has_usable_password(self) -> bool:
        return is_password_usable(self.password)

    def check_password(self, raw: str) -> bool:
        return self.has_usable_password() and check_password(raw, self.password)


class CustomerRequest(TimeStampedModel):
    """One "Request a change" or "Request to cancel" a customer sent about a trip (spec §9):
    the inbound message it became in the Inbox and the Customer Service task it opened.
    Also the rate limit's ledger. Nothing on the trip changes; staff act on the task."""

    class Kind(models.TextChoices):
        CHANGE = "change", "Change"
        CANCEL = "cancel", "Cancellation"

    account = models.ForeignKey(CustomerAccount, related_name="requests", on_delete=models.CASCADE)
    reservation = models.ForeignKey(
        "reservations.Reservation", related_name="customer_requests", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # The chosen `requests.CHANGE_TOPICS`, joined with ", "; blank for a cancellation.
    topics = models.CharField(max_length=255, blank=True)
    # One of `requests.CANCEL_REASONS`; blank for a change.
    reason = models.CharField(max_length=64, blank=True)
    message = models.OneToOneField(
        "messaging.Message", related_name="customer_request", null=True, on_delete=models.SET_NULL
    )
    task = models.OneToOneField(
        "tasks.Task", related_name="customer_request", null=True, on_delete=models.SET_NULL
    )

    def __str__(self) -> str:
        return f"{self.get_kind_display()} request · {self.account} · {self.reservation_id}"


class Throttle(models.Model):
    """One counter of the sign-in, sign-up and reset brakes (`apps.customers.throttle`):
    how many hits one IP address or email address has made in the window that ends at
    `expires_at`. Housekeeping, not history: rows are swept once their window runs out."""

    # SHA-256 of the scope and who is counted; the address itself is never stored.
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return f"{self.key[:12]} · {self.count}"
