"""Template context shared across every portal page (nav + notification bell)."""

from pathlib import Path

from django.conf import settings
from django.utils.functional import SimpleLazyObject

from apps.core.us_states import US_STATES
from apps.messaging.models import Conversation, Message
from apps.notifications.models import Notification

# Screens lifted from the prototype that aren't wired up yet — shown dimmed.
NAV_SOON = []


def _asset_version() -> str:
    """Cache-busting stamp = newest mtime of the hand-maintained local assets.

    Dev serves static files at a stable URL, so a browser keeps serving a cached
    app.js/app.css after an edit — which reads as "my change didn't work" (a stale
    app.js missing a new Alpine component leaves x-cloak'd widgets blank). Appending
    ?v=<mtime> makes the URL change with the file. Prod's manifest storage already
    hashes names, so this is a harmless no-op there.
    """
    root = Path(settings.BASE_DIR) / "static"
    try:
        return str(
            int(
                max(
                    (root / "css" / "app.css").stat().st_mtime,
                    (root / "js" / "app.js").stat().st_mtime,
                )
            )
        )
    except OSError:
        return "0"


def _inbox_unread_count() -> int:
    """Open conversations with at least one unread inbound message.

    Archived threads are excluded: dismissing a wrong number should stop it nagging
    the bell, not just hide it from the list.
    """
    return (
        Conversation.objects.filter(
            status=Conversation.Status.OPEN,
            messages__direction=Message.Direction.IN,
            messages__read_at__isnull=True,
        )
        .distinct()
        .count()
    )


def _airline_options() -> list[tuple[int, str]]:
    """Active carriers for the airline pickers (reservation editor, booking widget).
    Lazy: only pages that render a picker pay for the query."""
    from apps.addresses.models import Airline

    return [(a.pk, a.label) for a in Airline.objects.filter(is_active=True)]


def chrome(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {
            "nav_soon": NAV_SOON,
            "asset_version": _asset_version(),
            "address_bias_center": settings.ADDRESS_BIAS_CENTER,
            "us_states": US_STATES,
            "airline_options": SimpleLazyObject(_airline_options),
        }

    unread = Notification.objects.unread().select_related("lead", "lead__contact")
    return {
        "nav_soon": NAV_SOON,
        "asset_version": _asset_version(),
        "address_bias_center": settings.ADDRESS_BIAS_CENTER,
        "us_states": US_STATES,
        "unread_notifications": list(unread.order_by("-created_at")[:8]),
        "unread_count": unread.count(),
        "inbox_unread": _inbox_unread_count(),
        "airline_options": SimpleLazyObject(_airline_options),
    }
