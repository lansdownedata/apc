"""Every planner-account write, and the one rule for which weddings a planner sees (APC-66).

Invites and password-reset links are customer mail: they go through `send_html_email`
without `internal=True`, so the go-live switch (APC-90) gates them like any other send.
Invite and Resend are refused outright while the switch is off — with the same sentence
every other agent send uses — rather than creating an account nobody was told about.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.contacts.models import Contact
from apps.core import outbound
from apps.leads.models import Lead
from apps.notifications.email import send_html_email

from .models import PlannerAccount
from .tokens import planner_token


class PlannerError(Exception):
    """A refused planner-access action; the message is shown to staff."""


# --- links + mail ---------------------------------------------------------------------


def _link(name: str, account: PlannerAccount) -> str:
    path = reverse(
        name,
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": planner_token.make_token(account),
        },
    )
    return urljoin(settings.PUBLIC_BASE_URL, path)


def _mail_context(account: PlannerAccount) -> dict:
    return {
        "name": account.contact.name,
        "company_name": settings.COMPANY_NAME,
        "company_email": settings.COMPANY_EMAIL,
        "company_phone": settings.COMPANY_PHONE,
    }


def _send_invite(account: PlannerAccount) -> bool:
    return send_html_email(
        to=account.email,
        subject=f"Your wedding transportation with {settings.COMPANY_NAME}",
        template="planner_invite",
        context={**_mail_context(account), "register_url": _link("planners:register", account)},
    )


def _assert_outbound() -> None:
    if not outbound.enabled():
        raise PlannerError(outbound.OFF_MESSAGE)


# --- staff actions ---------------------------------------------------------------------


def invite(contact: Contact, *, actor) -> tuple[PlannerAccount, bool]:
    """Give `contact` a pending planner account and email the registration link.

    A revoked account is re-opened: a fresh invite, a fresh password set by the planner.
    Returns (account, email_sent); the account is kept when delivery fails so staff can
    use Resend.
    """
    _assert_outbound()
    email = (contact.email or "").strip().lower()
    if not email:
        raise PlannerError(f"Add an email address for {contact.name} before inviting them.")
    account = PlannerAccount.objects.filter(contact=contact).first()
    if account is not None and account.status == PlannerAccount.Status.REGISTERED:
        raise PlannerError(f"{contact.name} already has planner access.")
    if account is not None and account.status == PlannerAccount.Status.INVITED:
        raise PlannerError(f"{contact.name} is already invited. Use Resend to send a new link.")
    account = account or PlannerAccount(contact=contact)
    account.email = email
    account.set_unusable_password()
    account.invited_at = timezone.now()
    account.invited_by = actor
    account.registered_at = None
    account.revoked_at = None
    account.revoked_by = None
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError:
        raise PlannerError(f"{email} already signs in as another planner.") from None
    return account, _send_invite(account)


def resend(account: PlannerAccount) -> bool:
    """A new registration link. The previous one stops working (the token covers
    `invited_at`)."""
    _assert_outbound()
    if account.status != PlannerAccount.Status.INVITED:
        raise PlannerError("Only a pending invite can be resent.")
    account.invited_at = timezone.now()
    account.save(update_fields=["invited_at", "updated_at"])
    return _send_invite(account)


def revoke(account: PlannerAccount, *, actor) -> None:
    """End access now: the sign-in, every open session and every outstanding link."""
    if account.status == PlannerAccount.Status.REVOKED:
        return
    account.revoked_at = timezone.now()
    account.revoked_by = actor
    account.set_unusable_password()
    account.save(update_fields=["revoked_at", "revoked_by", "password", "updated_at"])


# --- the planner's own actions ---------------------------------------------------------


def register(account: PlannerAccount, password: str) -> PlannerAccount:
    """Set the password. The first time, this is registration."""
    account.set_password(password)
    fields = ["password", "updated_at"]
    if account.registered_at is None:
        account.registered_at = timezone.now()
        fields.append("registered_at")
    account.save(update_fields=fields)
    return account


def authenticate(email: str, password: str) -> PlannerAccount | None:
    """The account these credentials open, or None — the same None for an unknown email,
    a wrong password, a pending invite and a revoked account."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    account = PlannerAccount.objects.select_related("contact").filter(email=email).first()
    if account is None or not account.can_sign_in or not account.check_password(password):
        return None
    return account


def send_password_reset(email: str) -> bool:
    """Email a set-password link to a planner who can sign in. Silent otherwise: the page
    says the same thing either way, so it never confirms who has an account."""
    email = (email or "").strip().lower()
    account = PlannerAccount.objects.select_related("contact").filter(email=email).first()
    if account is None or not account.can_sign_in:
        return False
    return send_html_email(
        to=account.email,
        subject=f"Reset your {settings.COMPANY_NAME} planner password",
        template="planner_password_reset",
        context={**_mail_context(account), "reset_url": _link("planners:set_password", account)},
    )


# --- what a planner sees ----------------------------------------------------------------


def weddings_for(account: PlannerAccount) -> QuerySet[Lead]:
    """Every order the planner's contact holds any `LeadContact` role on, except lost ones.

    Derived from the roles on every call — adding a role adds the wedding, removing it
    takes it away — never stored on the account.
    """
    return (
        Lead.objects.filter(people__contact_id=account.contact_id)
        .exclude(status=Lead.Status.LOST)
        .distinct()
        .order_by("pk")
    )


def is_archived(trips, *, now=None) -> bool:
    """A wedding is archived once its last live trip is done: marked Done, or its scheduled
    end is past by the post-trip grace (statuses can be missing while LA runs in parallel).
    An order with no live trips yet is not archived — it is still being planned."""
    from apps.tasks.models import TaskConfig
    from apps.tasks.post_trip import order_finished_at

    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    return order_finished_at(list(trips), now=now or timezone.now(), grace=grace) is not None
