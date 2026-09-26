from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


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
    two_factor_enabled = models.BooleanField(default=False)
    can_manage_payments = models.BooleanField(
        "can manage payments",
        default=False,
        help_text="May run money actions (refunds, mark-paid, retry charges).",
    )
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
