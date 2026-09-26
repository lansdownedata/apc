from django.conf import settings
from django.db import models

from apps.accounts.models import Department
from apps.core.models import TimeStampedModel
from apps.dispatch.models import _split_list


def _owner(label: str) -> models.ForeignKey:
    return models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=f"{label} owner",
    )


class TaskConfig(models.Model):
    """Singleton — who owns each department's work by default, when overdue work escalates,
    and who gets the daily digest (APC-49). Mirrors `DispatchAlertConfig`.
    """

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(
        default=True, help_text="Turn task generation and escalation off without losing settings."
    )

    sales_owner = _owner("Sales")
    operations_owner = _owner("Operations")
    affiliate_mgmt_owner = _owner("Affiliate Management")
    customer_service_owner = _owner("Customer Service")
    accounting_owner = _owner("Accounting")

    overdue_grace_hours = models.PositiveIntegerField(
        default=24,
        help_text="How long a task can sit overdue before it escalates to the department "
        "owner and admins.",
    )
    digest_emails = models.TextField(
        blank=True,
        help_text="Who gets the daily overdue-task digest. One per line or comma-separated. "
        "Blank falls back to the company email.",
    )

    OWNER_FIELDS = {
        Department.SALES: "sales_owner",
        Department.OPERATIONS: "operations_owner",
        Department.AFFILIATE_MGMT: "affiliate_mgmt_owner",
        Department.CUSTOMER_SERVICE: "customer_service_owner",
        Department.ACCOUNTING: "accounting_owner",
    }

    class Meta:
        verbose_name = "task configuration"

    def __str__(self) -> str:
        return "Task configuration"

    @classmethod
    def load(cls) -> "TaskConfig":
        return cls.objects.get_or_create(pk=1)[0]

    def owner_for(self, department: str):
        """The department's default owner, or None."""
        return getattr(self, self.OWNER_FIELDS[department])

    @property
    def digest_list(self) -> list[str]:
        chosen = _split_list(self.digest_emails)
        return chosen or ([settings.COMPANY_EMAIL] if settings.COMPANY_EMAIL else [])


class TaskQuerySet(models.QuerySet):
    def unresolved(self):
        """Work still owed — open now or opening later."""
        return self.filter(status__in=Task.UNRESOLVED)


class Task(TimeStampedModel):
    """One unit of owned work on an order or one of its trips (APC-50).

    What the kind means lives in `definitions.KINDS`; `kind` has no DB choices so adding a
    kind never needs a migration.

    Uniqueness: the (lead, reservation, kind) constraint only bites trip-level rows —
    order-level rows have a NULL reservation, and NULLs never collide. `services.ensure_tasks`
    is what keeps those unique, under a lock on the lead.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Not open yet"
        OPEN = "open", "Open"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"
        NOT_APPLICABLE = "not_applicable", "Not applicable"

    UNRESOLVED = (Status.SCHEDULED, Status.OPEN)
    CLOSED = (Status.DONE, Status.SKIPPED, Status.NOT_APPLICABLE)

    kind = models.CharField(max_length=40)
    # Always set today. Phase E adds vendor-anchored tasks, so nothing beyond this column
    # should assume it.
    lead = models.ForeignKey("leads.Lead", related_name="tasks", on_delete=models.CASCADE)
    reservation = models.ForeignKey(
        "reservations.Reservation",
        related_name="tasks",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    department = models.CharField(max_length=32, choices=Department.choices)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="tasks",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    opens_at = models.DateTimeField()
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Null on a closed task = the system closed it (an auto-complete predicate).
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    escalated_tier = models.PositiveSmallIntegerField(default=0)
    note = models.TextField(blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta(TimeStampedModel.Meta):
        ordering = ["due_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["lead", "reservation", "kind"], name="one_task_per_kind"
            )
        ]
        indexes = [
            models.Index(fields=["status", "due_at"]),
            models.Index(fields=["assignee", "status"]),
        ]

    @property
    def definition(self):
        from .definitions import KINDS

        return KINDS.get(self.kind)

    @property
    def label(self) -> str:
        d = self.definition
        return d.label if d else self.kind

    @property
    def is_closed(self) -> bool:
        return self.status in self.CLOSED

    @property
    def closed_by_system(self) -> bool:
        return self.status == self.Status.DONE and self.completed_by_id is None

    def __str__(self) -> str:
        return f"{self.label} · {self.get_status_display()}"
