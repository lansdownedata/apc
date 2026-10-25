"""Planner sign-in, kept apart from Django's staff authentication (APC-66).

A planner session is two keys in the ordinary session — the account id and an HMAC of its
password hash — and nothing else. `django.contrib.auth` never hears about it: no
`_auth_user_id`, no `request.user`. That is the wall between planners and the staff portal:
every staff view is `@login_required`, and to it a signed-in planner is an anonymous visitor.

`planner_required` re-reads the account on every request, so Revoke and a new password
take effect on the very next page load rather than when a session happens to expire.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import quote

from django.db.models import F
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import PlannerAccount

SESSION_KEY = "planner_account_id"
HASH_SESSION_KEY = "planner_auth_hash"


def _session_hash(account: PlannerAccount) -> str:
    return salted_hmac(
        "apps.planners.auth.session", account.password, algorithm="sha256"
    ).hexdigest()


def sign_in(request, account: PlannerAccount) -> None:
    """Start a planner session. The session key is rotated (fixation), but a staff login
    in the same browser is left alone — the two never share keys."""
    request.session.cycle_key()
    request.session[SESSION_KEY] = account.pk
    request.session[HASH_SESSION_KEY] = _session_hash(account)
    account.last_login = timezone.now()
    account.save(update_fields=["last_login"])


def sign_out(request) -> None:
    for key in (SESSION_KEY, HASH_SESSION_KEY):
        request.session.pop(key, None)
    request.session.cycle_key()


def current_planner(request) -> PlannerAccount | None:
    """The signed-in planner, or None. A session whose account was revoked or whose
    password changed since sign-in is ended here."""
    pk = request.session.get(SESSION_KEY)
    if pk is None:
        return None
    account = PlannerAccount.objects.select_related("contact").filter(pk=pk).first()
    stored = request.session.get(HASH_SESSION_KEY, "")
    if (
        account is None
        or not account.can_sign_in
        or not constant_time_compare(stored, _session_hash(account))
    ):
        sign_out(request)
        return None
    return account


def _log_view(account: PlannerAccount) -> None:
    now = timezone.now()
    PlannerAccount.objects.filter(pk=account.pk).update(
        last_seen_at=now, view_count=F("view_count") + 1
    )
    account.last_seen_at = now


def planner_required(view):
    """Only a signed-in planner gets through; everyone else (a staff session included) is
    sent to the planner sign-in. Sets `request.planner`."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        account = current_planner(request)
        if account is None:
            nxt = quote(request.get_full_path(), safe="/")
            return redirect(f"{reverse('planners:login')}?next={nxt}")
        request.planner = account
        if request.method == "GET":
            _log_view(account)
        return view(request, *args, **kwargs)

    return wrapped
