from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import OuterRef, Subquery
from django.utils import dateformat, timezone

from apps.accounts.models import Department
from apps.core.models import TimeStampedModel
from apps.dispatch.models import _split_list


def format_local(moment: datetime | None, fmt: str = "M j, g:i A") -> str:
    """An already-localised aware datetime as `Sep 4, 7:30 AM EDT`."""
    if moment is None:
        return ""
    return f"{dateformat.format(moment, fmt)} {moment.tzname()}"


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
    post_trip_grace_hours = models.PositiveIntegerField(
        default=2,
        help_text="How long after a trip's scheduled end, with no Done status, before its "
        "post-trip review opens anyway.",
    )
    # ⚠ PLACEHOLDERS (APC-59): the client hasn't answered how overtime is counted
    # (Confluence 46006273 §9 "Still open" #2). Both are settings so his answer is an
    # edit on the Tasks settings screen, not a deploy. The suggestion only — a person
    # always decides what's billable.
    overtime_increment_minutes = models.PositiveIntegerField(
        default=15,
        validators=[MinValueValidator(1)],
        help_text="Suggested overtime rounds up to this many minutes. Placeholder until the "
        "client confirms.",
    )
    overtime_grace_minutes = models.PositiveIntegerField(
        default=15,
        help_text="Minutes over the billed hours before any overtime is suggested. "
        "Placeholder until the client confirms.",
    )
    future_booking_offset_days = models.PositiveIntegerField(
        default=330,
        help_text="Days after an order's last trip that Sales follows up about booking again "
        "(330 is about a wedding's first anniversary).",
    )
    digest_emails = models.TextField(
        blank=True,
        help_text="Who gets the daily overdue-task digest. One per line or comma-separated. "
        "Blank falls back to the company email.",
    )
    # The business-timezone date the overdue digest last went out (APC-52) — the
    # once-a-day guard, so a 15-minute cron can't send it 96 times.
    digest_sent_on = models.DateField(null=True, blank=True, editable=False)

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

    def with_order_tz(self):
        """Annotate `order_tz`: the zone of the order's first pickup, which is what an
        order-level task's due date is anchored on — one subquery, not a lookup per row."""
        from apps.reservations.models import Reservation

        first = Reservation.objects.filter(lead_id=OuterRef("lead_id")).order_by(
            "pickup_date", "pickup_time", "pk"
        )
        return self.annotate(order_tz=Subquery(first.values("pickup_timezone")[:1]))


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
    # An order task (lead set) or a vendor task (vendor set, APC-71) — never both, never
    # neither (the `task_order_or_vendor` check).
    lead = models.ForeignKey(
        "leads.Lead", related_name="tasks", null=True, blank=True, on_delete=models.CASCADE
    )
    vendor = models.ForeignKey(
        "vendors.Vendor", related_name="tasks", null=True, blank=True, on_delete=models.CASCADE
    )
    # The policy an `insurance_renewal` task chases (APC-71) — one task per policy.
    insurance = models.ForeignKey(
        "vendors.VendorInsurance",
        related_name="tasks",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
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
            ),
            models.UniqueConstraint(fields=["insurance", "kind"], name="one_task_per_policy"),
            models.CheckConstraint(
                condition=models.Q(lead__isnull=False, vendor__isnull=True)
                | models.Q(lead__isnull=True, vendor__isnull=False),
                name="task_order_or_vendor",
            ),
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
    def tz_name(self) -> str:
        """The zone this task's times display in: its trip's, or for an order-level task
        the first trip's (`with_order_tz`). Never the viewer's."""
        if self.reservation_id:
            zone = self.reservation.pickup_timezone
        else:
            zone = getattr(self, "order_tz", None) or ""
        return zone or settings.TIME_ZONE

    def local(self, moment: datetime | None) -> datetime | None:
        return moment.astimezone(ZoneInfo(self.tz_name)) if moment else None

    @property
    def due_local(self) -> datetime | None:
        return self.local(self.due_at)

    @property
    def due_display(self) -> str:
        """`Sep 4, 7:30 AM EDT` in the task's trip zone, abbreviation always shown. Use
        this, never `due_at|date` — the date filter renders in TIME_ZONE, not the trip's."""
        return format_local(self.due_local)

    @property
    def opens_display(self) -> str:
        return format_local(self.local(self.opens_at))

    @property
    def completed_display(self) -> str:
        return format_local(self.local(self.completed_at))

    @property
    def is_overdue_now(self) -> bool:
        return (
            self.status == self.Status.OPEN
            and self.due_at is not None
            and self.due_at < timezone.now()
        )

    @property
    def department_label(self) -> str:
        return self.get_department_display()

    @property
    def pickup_display(self) -> str:
        """The pickup this task is about, in that trip's zone: its own trip, or for an
        order-level task the order's first trip (from `queue.queue_for`'s annotations —
        without them an order-level row shows nothing rather than querying)."""
        if self.reservation_id:
            at = self.reservation.pickup_at
            return format_local(at) if at else ""
        day = getattr(self, "order_pickup_date", None)
        if day is None:
            return ""
        clock = getattr(self, "order_pickup_time", None) or time(0, 0)
        return format_local(datetime.combine(day, clock, tzinfo=ZoneInfo(self.tz_name)))

    @property
    def is_closed(self) -> bool:
        return self.status in self.CLOSED

    @property
    def closed_by_system(self) -> bool:
        return self.status == self.Status.DONE and self.completed_by_id is None

    def __str__(self) -> str:
        return f"{self.label} · {self.get_status_display()}"
