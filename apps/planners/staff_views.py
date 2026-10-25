"""Staff controls for planner access on the contact profile (APC-66).

Under `/portal/contacts/<pk>/planner/…`, `@login_required` like every staff view. Any
agent may invite: the ticket puts the action with the agent who works the wedding.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.contacts.models import Contact

from . import services
from .models import PlannerAccount


def _back(contact: Contact):
    return redirect("contact_detail", pk=contact.pk)


def _sent(request, sent: bool, email: str) -> None:
    if sent:
        messages.success(request, f"Invite sent to {email}.")
    else:
        messages.warning(request, "The invite email could not be sent. Use Resend to try again.")


@login_required
@require_POST
def invite(request, pk: int):
    contact = get_object_or_404(Contact, pk=pk)
    try:
        account, sent = services.invite(contact, actor=request.user)
    except services.PlannerError as exc:
        messages.error(request, str(exc))
        return _back(contact)
    _sent(request, sent, account.email)
    return _back(contact)


@login_required
@require_POST
def resend(request, pk: int):
    account = get_object_or_404(PlannerAccount.objects.select_related("contact"), contact_id=pk)
    try:
        sent = services.resend(account)
    except services.PlannerError as exc:
        messages.error(request, str(exc))
        return _back(account.contact)
    _sent(request, sent, account.email)
    return _back(account.contact)


@login_required
@require_POST
def revoke(request, pk: int):
    account = get_object_or_404(PlannerAccount.objects.select_related("contact"), contact_id=pk)
    services.revoke(account, actor=request.user)
    messages.success(request, f"{account.contact.name} can no longer sign in.")
    return _back(account.contact)
