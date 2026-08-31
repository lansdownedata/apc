"""Template context shared across every public marketing page."""

from django.conf import settings
from django.templatetags.static import static


def canonical(request):
    """Query-free absolute canonical URL for the current page.

    `request.build_absolute_uri()` with no argument includes the query string,
    so campaign traffic (e.g. `/fleet/?utm_source=x`) would emit a canonical
    pointing at the parameterized URL — defeating canonicalization. Passing
    `request.path` strips it.
    """
    return {"canonical_url": request.build_absolute_uri(request.path)}


def social_card(request):
    """Absolute URL of the default link-preview image.

    og:image must be absolute — a scraper has no page to resolve `/static/…`
    against — and it has to survive the domain move off herokuapp.com, so it is
    built from the live request rather than a hardcoded host. Overriding it for a
    single page is `{% block og_image %}` in `public/base_public.html`.
    """
    return {"og_image_url": request.build_absolute_uri(static("public/og/og-card.png"))}


# Zones offered in the booking panel's picker. Curated rather than the full ~600 of
# the IANA database, which is unreadable as a dropdown and mostly irrelevant to a
# Washington-area charter company. A visitor whose detected zone is not on this list
# gets it added client-side, so nobody is forced into the wrong one.
BOOKING_TIMEZONES = (
    ("America/New_York", "Eastern — New York"),
    ("America/Chicago", "Central — Chicago"),
    ("America/Denver", "Mountain — Denver"),
    ("America/Phoenix", "Mountain, no DST — Phoenix"),
    ("America/Los_Angeles", "Pacific — Los Angeles"),
    ("America/Anchorage", "Alaska — Anchorage"),
    ("Pacific/Honolulu", "Hawaii — Honolulu"),
    ("America/Toronto", "Eastern — Toronto"),
    ("Europe/London", "United Kingdom — London"),
    ("Europe/Paris", "Central Europe — Paris"),
    ("Asia/Dubai", "Gulf — Dubai"),
    ("Asia/Tokyo", "Japan — Tokyo"),
    ("Australia/Sydney", "Australia — Sydney"),
)


def site_settings(request):
    """Client-provided public embeds (Calendly link, WeddingWire widget snippet)."""
    return {
        "calendly_url": settings.CALENDLY_URL,
        "weddingwire_widget": settings.WEDDINGWIRE_WIDGET,
        # Rendered server-side on purpose: an Alpine x-for inside a <select> is invalid
        # HTML, the parser hoists it out, and Tom Select initialises with no options.
        "timezone_options": BOOKING_TIMEZONES,
    }
