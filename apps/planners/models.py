"""Wedding planner accounts (APC-66).

A planner is NOT an `accounts.User`. Staff users and planners are kept apart at the model
level: a planner signs in through `apps.planners.auth`, which keeps its own session keys and
never sets `request.user`. Every staff view is guarded by `@login_required` and so sees a
signed-in planner as an anonymous visitor, and no `User` queryset (assignee pickers, the user
list, digests) can ever pick a planner up.

The account carries who the planner is and how they sign in — never which weddings they
see. That is derived from `LeadContact` roles every time (`services.weddings_for`).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.hashers import check_password, is_password_usable, make_password
from django.db import models

from apps.core.models import TimeStampedModel


class PlannerAccount(TimeStampedModel):
    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        REGISTERED = "registered", "Registered"
        REVOKED = "revoked", "Revoked"

    contact = models.OneToOneField(
        "contacts.Contact", related_name="planner_account", on_delete=models.CASCADE
    )
    # Snapshot of the contact's email at invite time: it is the sign-in name, so a later
    # edit to the contact must not silently change how someone logs in.
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=128)
    invited_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Sign-ins and page views, logged the way the quote page logs its views.
    last_login = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)

    class Meta(TimeStampedModel.Meta):
        verbose_name = "planner account"

    def __str__(self) -> str:
        return self.email

    @property
    def status(self) -> str:
        """Derived, never stored. Revoked wins over everything before it."""
        if self.revoked_at is not None:
            return self.Status.REVOKED
        if self.registered_at is not None:
            return self.Status.REGISTERED
        return self.Status.INVITED

    @property
    def can_sign_in(self) -> bool:
        return self.status == self.Status.REGISTERED and self.has_usable_password()

    # --- the bits of the user API Django's password tooling relies on ---------------

    def set_password(self, raw: str) -> None:
        self.password = make_password(raw)

    def set_unusable_password(self) -> None:
        self.password = make_password(None)

    def has_usable_password(self) -> bool:
        return is_password_usable(self.password)

    def check_password(self, raw: str) -> bool:
        return self.has_usable_password() and check_password(raw, self.password)

    @classmethod
    def get_email_field_name(cls) -> str:
        return "email"
