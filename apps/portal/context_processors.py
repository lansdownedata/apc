"""Template context shared across every portal page (nav + notification bell)."""

from apps.notifications.models import Notification

# Screens lifted from the prototype that aren't wired up yet — shown dimmed.
NAV_SOON = [
    ("Inbox", "ti-inbox"),
    ("Pipeline", "ti-layout-kanban"),
    ("Contacts", "ti-address-book"),
    ("Reviews", "ti-star"),
    ("Settings", "ti-settings"),
]


def chrome(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"nav_soon": NAV_SOON}

    unread = Notification.objects.unread().select_related("lead", "lead__contact")
    return {
        "nav_soon": NAV_SOON,
        "unread_notifications": list(unread.order_by("-created_at")[:8]),
        "unread_count": unread.count(),
    }
