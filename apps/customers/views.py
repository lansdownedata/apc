"""Customer portal pages. Never `@login_required`: customers have their own session
(`apps.customers.auth`)."""

from django.http import HttpRequest, HttpResponse


def login(request: HttpRequest) -> HttpResponse:
    """Stand-in so `customer_required` has somewhere to send visitors; the sign-in page
    replaces it in the portal's sign-in task."""
    return HttpResponse()


def link_page(request: HttpRequest, **kwargs: str) -> HttpResponse:
    """Stand-in for the pages the account emails link to (register, finish sign-up, set a
    new password), so the links can be built; the portal's sign-in task replaces it."""
    return HttpResponse()
