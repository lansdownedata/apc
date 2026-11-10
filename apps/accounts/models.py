from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.utils import timezone


class Department(models.TextChoices):
    """Who owns a piece of work (APC-49). Routes tasks; grants no permissions (D1)."""

    SALES = "sales", "Sales"
    OPERATIONS = "operations", "Operations"
    AFFILIATE_MGMT = "affiliate_mgmt", "Affiliate Management"
    CUSTOMER_SERVICE = "customer_service", "Customer Service"
    ACCOUNTING = "accounting", "Accounting"


class UserQuerySet(models.QuerySet):
    def in_department(self, department: str) -> "UserQuerySet":
        return self.filter(departments__department=department)


class UserAccountManager(UserManager.from_queryset(UserQuerySet)):
    pass


class User(AbstractUser):
    """Owner/admin and agent accounts for the Lead Manager.

    Custom user defined up front so AUTH_USER_MODEL is stable before the first
    migration (swapping it later is painful).
    """

    class Role(models.TextChoices):
        OWNER_ADMIN = "owner_admin", "Admin"
        AGENT = "agent", "Agent"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        DEACTIVATED = "deactivated", "Deactivated"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(max_length=32, blank=True)
    can_manage_payments = models.BooleanField(
        "can manage payments",
        default=False,
        help_text="May run money actions (refunds, mark-paid, retry charges).",
    )
    # Per-user screen settings, e.g. {"processing": {"ready_sort": "customer"}} (APC-102).
    preferences = models.JSONField(default=dict, blank=True)
    invited_at = models.DateTimeField(null=True, blank=True)
    invite_accepted_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invitees",
    )

    objects = UserAccountManager()

    @property
    def status(self) -> str:
        """Derived, never stored — a stored status drifts out of sync with is_active.

        is_active is checked first: a deactivated account is deactivated whatever its
        invite state. A user with no invited_at predates invites and reads as Active.
        """
        if not self.is_active:
            return self.Status.DEACTIVATED
        if self.invited_at and not self.invite_accepted_at:
            return self.Status.PENDING
        return self.Status.ACTIVE

    @property
    def invite_expires_at(self) -> datetime | None:
        """When the latest invite link stops working. The link is a default_token_generator
        token, so its window is PASSWORD_RESET_TIMEOUT counted from the send."""
        if not self.invited_at:
            return None
        return self.invited_at + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT)

    @property
    def invite_expired(self) -> bool:
        expires = self.invite_expires_at
        return expires is not None and expires <= timezone.now()

    @property
    def has_payments_access(self) -> bool:
        return self.is_owner_admin or self.can_manage_payments

    @property
    def is_owner_admin(self) -> bool:
        return self.role == self.Role.OWNER_ADMIN

    @property
    def department_list(self) -> list[str]:
        """Department values in the order they were granted."""
        return list(self.departments.order_by("pk").values_list("department", flat=True))

    def __str__(self) -> str:
        return self.get_full_name() or self.username


class UserDepartment(models.Model):
    """One membership row per (user, department) — a table rather than a JSON list so the
    task queue can filter on it with a plain join."""

    user = models.ForeignKey(User, related_name="departments", on_delete=models.CASCADE)
    department = models.CharField(max_length=32, choices=Department.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "department"], name="one_membership_per_dept")
        ]

    def __str__(self) -> str:
        return f"{self.user} · {self.get_department_display()}"


class SignInCode(models.Model):
    """A one-time sign-in code for a staff user or a planner account. Only a keyed hash of the
    code is stored. `consumed_at` is set on success and when the code is voided (replaced, or
    five wrong tries)."""

    class Channel(models.TextChoices):
        SMS = "sms", "Text"
        EMAIL = "email", "Email"

    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="sign_in_codes"
    )
    planner = models.ForeignKey(
        "planners.PlannerAccount",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="sign_in_codes",
    )
    code_hash = models.CharField(max_length=64)
    channel = models.CharField(max_length=8, choices=Channel.choices)
    podium_message_uid = models.CharField(max_length=64, blank=True, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, planner__isnull=True)
                    | models.Q(user__isnull=True, planner__isnull=False)
                ),
                name="sign_in_code_one_owner",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_channel_display()} code for {self.user_id or self.planner_id}"
