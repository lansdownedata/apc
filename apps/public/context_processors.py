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


def site_settings(request):
    """Client-provided public embeds (Calendly link, WeddingWire widget snippet)."""
    return {
        "calendly_url": settings.CALENDLY_URL,
        "weddingwire_widget": settings.WEDDINGWIRE_WIDGET,
    }
