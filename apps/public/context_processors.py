"""Template context shared across every public marketing page."""

from django.conf import settings


def canonical(request):
    """Query-free absolute canonical URL for the current page.

    `request.build_absolute_uri()` with no argument includes the query string,
    so campaign traffic (e.g. `/fleet/?utm_source=x`) would emit a canonical
    pointing at the parameterized URL — defeating canonicalization. Passing
    `request.path` strips it.
    """
    return {"canonical_url": request.build_absolute_uri(request.path)}


def site_settings(request):
    """Client-provided public embeds (Calendly link, WeddingWire widget snippet)."""
    return {
        "calendly_url": settings.CALENDLY_URL,
        "weddingwire_widget": settings.WEDDINGWIRE_WIDGET,
    }
