from django.conf import settings
from django.db import models

from apps.accounts.models import Department
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
