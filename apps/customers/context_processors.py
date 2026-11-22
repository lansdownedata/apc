"""The signed-in customer, for the header the website and the portal share."""

from __future__ import annotations

from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from .auth import SESSION_KEY, current_customer
from .models import CustomerAccount


def who(account: CustomerAccount) -> dict:
    """What the account menu prints: the name, its first word and up to two initials."""
    words = account.contact.name.split()
    initials = (words[0][0] + (words[-1][0] if len(words) > 1 else "")) if words else "A"
    return {
        "name": " ".join(words) or account.email,
        "first_name": words[0] if words else "Account",
        "initials": initials.upper(),
        "email": account.email,
    }


def _site_customer(request: HttpRequest) -> dict | None:
    account = getattr(request, "customer", None)
    if account is None and getattr(request, "session", None) is not None:
        if SESSION_KEY in request.session:
            account = current_customer(request)
    return who(account) if account else None


def site_customer(request: HttpRequest) -> dict:
    """`site_customer` on every page, read only when a template prints it: a portal page
    reuses the account `customer_required` loaded, a website page costs one query for a
    signed-in customer and none for a visitor, and the staff portal never asks."""
    return {"site_customer": SimpleLazyObject(lambda: _site_customer(request))}
