"""Response hardening from Intuit's app security requirements (QuickBooks production keys).

* Only the methods the app uses are answered. TRACE, TRACK, PUT, PATCH, DELETE and OPTIONS
  get a 405 before any view runs.
* Nothing sensitive is cached. Every response carries `no-cache, no-store` except an
  anonymous visit to a plain marketing page (`public` namespace, no token or slug in the
  URL). A signed-in page therefore always goes back to the server, which checks the login
  again, including after the Back button following a sign-out.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.utils.cache import add_never_cache_headers

ALLOWED_METHODS = ("GET", "HEAD", "POST")


class MethodGuardMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method not in ALLOWED_METHODS:
            return HttpResponseNotAllowed(ALLOWED_METHODS)
        return self.get_response(request)


def _cacheable(request: HttpRequest) -> bool:
    match = request.resolver_match
    if match is None or match.namespace != "public" or match.kwargs:
        return False
    user = getattr(request, "user", None)
    return not (user and user.is_authenticated)


class NoStoreMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not _cacheable(request):
            add_never_cache_headers(response)
        return response
