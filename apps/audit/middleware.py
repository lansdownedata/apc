"""Opens the audit frame for every request (APC-140, spec §4.3).

Actor and source are lambdas: `request.user` and `request.resolver_match` are only settled
once the view runs, and a request that writes nothing should never load the user.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from django.http import HttpRequest, HttpResponse

from .context import audit_context, new_cid

if TYPE_CHECKING:
    from apps.accounts.models import User

WEBHOOKS = {
    "stripe_webhook": "stripe",
    "podium_webhook": "podium",
    "la_webhook": "limoanywhere",
    "calendly_webhook": "calendly",
    "gnet_farm_in": "gnet",
    "gnet_callback": "gnet",
}
TRIP_CONFIRMATIONS = {"trip_confirm", "order_trip_confirm", "wedding_trip_confirm"}


def source_for(request: HttpRequest) -> str:
    """The audit `source` label for the request's route."""
    if request.path.startswith("/portal/"):
        return "portal"
    if request.path.startswith("/admin/"):  # /admin/login/ is a redirect outside the admin
        return "admin"
    match = getattr(request, "resolver_match", None)
    if match is None:
        return f"web: {request.path}"
    namespaces = match.namespaces
    name = match.url_name
    if "admin" in namespaces:
        return "admin"
    if name == "cron_job":
        return f"cron: {match.kwargs['job']}"
    if name in WEBHOOKS:
        return f"webhook: {WEBHOOKS[name]}"
    if name and name.startswith("quote_"):
        return "customer: quote page"
    if name in TRIP_CONFIRMATIONS:
        return "customer: trip confirmation"
    if name == "wedding_details":
        return "customer: wedding details"
    if name == "affiliate_trip_confirm":
        return "affiliate: trip confirmation"
    if "planners" in namespaces:
        return "planner portal"
    if "public" in namespaces:
        return "public site"
    if name == "accept_invite":
        return "invite"
    return f"web: {name or request.path}"


def _user(request: HttpRequest) -> User | None:
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def _planner_email(request: HttpRequest) -> str:
    planner = getattr(request, "planner", None)
    return planner.email if planner else ""


class AuditMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        with audit_context(
            actor=lambda: _user(request),
            source=lambda: source_for(request),
            actor_email=lambda: _planner_email(request),
            cid=f"r-{new_cid()}",
        ):
            return self.get_response(request)
