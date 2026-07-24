"""Template context shared across every public marketing page."""


def canonical(request):
    """Query-free absolute canonical URL for the current page.

    `request.build_absolute_uri()` with no argument includes the query string,
    so campaign traffic (e.g. `/fleet/?utm_source=x`) would emit a canonical
    pointing at the parameterized URL — defeating canonicalization. Passing
    `request.path` strips it.
    """
    return {"canonical_url": request.build_absolute_uri(request.path)}
