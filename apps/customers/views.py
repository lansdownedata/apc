"""Customer portal pages. Never `@login_required`: customers have their own session
(`apps.customers.auth`)."""

from django.http import HttpRequest, HttpResponse


def login(request: HttpRequest) -> HttpResponse:
    """Stand-in so `customer_required` has somewhere to send visitors; the sign-in page
    replaces it in the portal's sign-in task."""
    return HttpResponse()
