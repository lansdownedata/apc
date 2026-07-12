"""Template context shared across every portal page (nav + notification bell)."""

from apps.leads.models import Lead
from apps.messaging.models import Message
from apps.notifications.models import Notification

# Screens lifted from the prototype that aren't wired up yet — shown dimmed.
NAV_SOON = [
    ("Contacts", "ti-address-book"),
    ("Reviews", "ti-star"),
    ("Settings", "ti-settings"),
]


def _inbox_unread_count() -> int:
    """Distinct leads with at least one unread inbound message."""
    return (
        Lead.objects.filter(
            messages__direction=Message.Direction.IN, messages__read_at__isnull=True
        )
        .distinct()
        .count()
    )


def chrome(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"nav_soon": NAV_SOON}

    unread = Notification.objects.unread().select_related("lead", "lead__contact")
    return {
        "nav_soon": NAV_SOON,
        "unread_notifications": list(unread.order_by("-created_at")[:8]),
        "unread_count": unread.count(),
        "inbox_unread": _inbox_unread_count(),
    }
