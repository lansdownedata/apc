"""Staff controls for customer portal access on the contact profile (spec 2026-11-20 §12).

Under `/portal/contacts/<pk>/customer/…`, `@login_required` like every staff view, and
open to any agent as the planner access card's are (`apps/planners/staff_views.py`). A
signed-in customer is not a `User`, so `@login_required` sends them to the staff sign-in.
Every refusal is a flash message back on the profile.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.contacts.models import Contact

from . import services
from .models import CustomerAccount


def _back(contact_pk: int) -> HttpResponseRedirect:
    return redirect("contact_detail", pk=contact_pk)


def _account(pk: int) -> CustomerAccount:
    return get_object_or_404(CustomerAccount.objects.select_related("contact"), contact_id=pk)


def _sent(request: HttpRequest, sent: bool, email: str) -> None:
    if sent:
        messages.success(request, f"Invite sent to {email}.")
    else:
        messages.warning(request, "The invite email could not be sent. Use Resend to try again.")


@login_required
@require_POST
def invite(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    contact = get_object_or_404(Contact.objects.select_related("company"), pk=pk)
    try:
        account, sent = services.invite(contact, actor=request.user)
    except services.CustomerError as exc:
        messages.error(request, str(exc))
        return _back(pk)
    _sent(request, sent, account.email)
    return _back(pk)


@login_required
@require_POST
def resend(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    account = _account(pk)
    try:
        sent = services.resend(account)
    except services.CustomerError as exc:
        messages.error(request, str(exc))
        return _back(pk)
    _sent(request, sent, account.email)
    return _back(pk)


@login_required
@require_POST
def revoke(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    account = _account(pk)
    services.revoke(account, actor=request.user)
    messages.success(
        request, f"{account.contact.name} can no longer sign in to the customer portal."
    )
    return _back(pk)


@login_required
@require_POST
def reset(request: HttpRequest, pk: int) -> HttpResponseRedirect:
    account = _account(pk)
    try:
        sent = services.staff_password_reset(account)
    except services.CustomerError as exc:
        messages.error(request, str(exc))
        return _back(pk)
    if sent:
        messages.success(request, f"Password reset sent to {account.email}.")
    else:
        messages.warning(request, "The reset email could not be sent. Try again in a minute.")
    return _back(pk)
