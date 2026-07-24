from django.views.generic.base import RedirectView


def perma(pattern_name: str) -> RedirectView:
    """Build a 301 (permanent) redirect view to a named URL.

    Used to preserve link equity from dead WordPress URLs by pointing them
    at the closest live page.
    """
    return RedirectView.as_view(pattern_name=pattern_name, permanent=True)
