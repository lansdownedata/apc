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
from django.contrib.auth.hashers import make_password
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


LOCKOUT_FAILURES = 10
LOCKOUT_FOR = timedelta(minutes=15)


def register(account: PlannerAccount, password: str) -> PlannerAccount:
    """Set the password. The first time, this is registration. Clears any sign-in lock:
    whoever holds a valid link has proved they own the address."""
    account.set_password(password)
    account.failed_logins = 0
    account.locked_until = None
    fields = ["password", "failed_logins", "locked_until", "updated_at"]
    if account.registered_at is None:
        account.registered_at = timezone.now()
        fields.append("registered_at")
    account.save(update_fields=fields)
    return account


def _record_failure(account: PlannerAccount, now) -> None:
    failures = account.failed_logins + 1
    if failures >= LOCKOUT_FAILURES:
        PlannerAccount.objects.filter(pk=account.pk).update(
            failed_logins=0, locked_until=now + LOCKOUT_FOR
        )
    else:
        PlannerAccount.objects.filter(pk=account.pk).update(failed_logins=failures)


def authenticate(email: str, password: str) -> PlannerAccount | None:
    """The account these credentials open, or None — the same None, in about the same
    time, for an unknown email, a pending invite, a revoked or locked account and a wrong
    password. Like Django's ModelBackend, the hasher runs exactly once on every path, so
    response time doesn't say whether an address has an account."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    now = timezone.now()
    account = PlannerAccount.objects.select_related("contact").filter(email=email).first()
    locked = account is not None and account.locked_until is not None and account.locked_until > now
    if account is None or not account.can_sign_in or locked:
        make_password(password)  # the same hashing cost as checking a real password
        return None
    if not account.check_password(password):
        _record_failure(account, now)
        return None
    if account.failed_logins or account.locked_until:
        account.failed_logins, account.locked_until = 0, None
        account.save(update_fields=["failed_logins", "locked_until"])
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


# --- request a change (APC-68) --------------------------------------------------------

REQUEST_LIMIT = 10  # per planner account, per rolling hour
REQUEST_WINDOW = timedelta(hours=1)
MAX_REQUEST_LENGTH = 2000


class RequestRefused(Exception):
    """A change request that can't be taken; `reason` picks the page state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def over_limit(account: PlannerAccount) -> bool:
    since = timezone.now() - REQUEST_WINDOW
    return account.requests.filter(created_at__gte=since).count() >= REQUEST_LIMIT


def request_change(account: PlannerAccount, lead: Lead, *, about: str, body: str):
    """Record a planner's request as an inbound message in their own conversation and open
    a Customer Service task for it. The caller has already checked the planner holds a
    role on `lead` and that the wedding isn't archived.

    The message names the wedding and what it's about, so it reads on its own in the
    Inbox. An archived conversation stays archived (archive never reopens) — the message
    is still recorded and the task still opens, so the request can't be lost.
    """
    from apps.messaging import services as messaging
    from apps.messaging.models import Message
    from apps.tasks import services as tasks

    from .models import PlannerRequest

    text = (body or "").strip()[:MAX_REQUEST_LENGTH]
    if not text:
        raise RequestRefused("blank")
    if over_limit(account):
        raise RequestRefused("limited")
    title = lead.wedding_name.strip() or lead.contact.name
    about_line = about or "the whole day"
    composed = f"Planner request · {title} ({lead.quote_no})\nAbout: {about_line}\n\n{text}"
    with transaction.atomic():
        conversation = messaging.conversation_for(account.contact)
        message = messaging.record_inbound(
            conversation,
            channel=Message.Channel.PORTAL,
            body=composed,
            sender_name=account.contact.name,
        )
        task = tasks.open_request_task(lead, "planner_change_request")
        return PlannerRequest.objects.create(
            account=account, lead=lead, message=message, task=task, about=about[:255]
        )
