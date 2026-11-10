"""What needs attention in Settings — one list for the Overview page and the menu's red dots."""

from dataclasses import dataclass

from django.conf import settings

from apps.dispatch.models import DispatchAlertConfig
from apps.fleet.models import RENEWAL_PREFETCH, Driver, Vehicle
from apps.messaging.models import NotificationConfig
from apps.settings.menu import label_for
from apps.tasks.models import TaskConfig


@dataclass(frozen=True)
class Attention:
    key: str
    title: str
    detail: str
    url_name: str | None
    severity: str  # "danger" | "warn"

    @property
    def link_label(self) -> str | None:
        """The menu label of the page that fixes this, or None when no page does."""
        return label_for(self.url_name) if self.url_name else None


def fleet_attention_count() -> int:
    """Active drivers + units whose paperwork is lapsed or expiring — the Overview's fleet row."""
    subjects = [
        *Driver.objects.filter(status=Driver.Status.ACTIVE).prefetch_related(RENEWAL_PREFETCH),
        *Vehicle.objects.filter(status=Vehicle.Status.ACTIVE).prefetch_related(RENEWAL_PREFETCH),
    ]
    return sum(1 for s in subjects if s.needs_attention)


def settings_attention() -> list[Attention]:
    """Everything that is switched off or lapsed, in the fixed order the page lists it."""
    from apps.integrations import qbo

    rows: list[Attention] = []
    if not NotificationConfig.load().enabled:
        rows.append(
            Attention(
                "messages_off",
                "Automated messages are off",
                "No customer or affiliate touch-points send until this is on.",
                "messages",
                "danger",
            )
        )
    if not TaskConfig.load().enabled:
        rows.append(
            Attention(
                "tasks_off",
                "Tasks are off",
                "No tasks are created or escalated until this is on.",
                "task_settings",
                "danger",
            )
        )
    if not DispatchAlertConfig.load().enabled:
        rows.append(
            Attention(
                "dispatch_off",
                "Dispatch monitor is off",
                "Missed-milestone alerts will not fire.",
                "dispatch_alerts",
                "danger",
            )
        )
    fleet = fleet_attention_count()
    if fleet > 0:
        subject = "driver or vehicle has" if fleet == 1 else "drivers or vehicles have"
        rows.append(
            Attention(
                "fleet",
                f"{fleet} {subject} lapsed or expiring paperwork",
                "Renewals on file are expired or due soon.",
                "renewal_type_list",
                "danger",
            )
        )
    if qbo.enabled() and not qbo.is_connected():
        rows.append(
            Attention(
                "qbo",
                "QuickBooks is not connected",
                "Billing accounts and invoices cannot sync until it is connected.",
                "quickbooks_settings",
                "danger",
            )
        )
    if not settings.OUTBOUND_ENABLED:
        rows.append(
            Attention(
                "not_live",
                "Not live",
                "Customers and affiliates receive nothing. "
                "Turned on once, at go-live, by your developer.",
                None,
                "warn",
            )
        )
    return rows
