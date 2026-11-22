"""Every customer-account write: invite, self sign-up, sign-in, password reset and change.

Mirrors `apps/planners/services.py`. The three account emails (`customer_invite`,
`customer_signup`, `customer_password_reset`) are message rows sent through the shared
sender as customer mail, so the go-live switch (APC-90) gates them like any other send.
Staff actions are refused outright while the switch is off, with the same sentence every
other agent send uses, rather than creating an account nobody was told about.

Self sign-up writes nothing until the email is proved: `start_signup` only mails a signed
link carrying the form, and `complete_signup` creates or attaches the account when the link
is used. Nothing either returns or raises says whether an email already has an account.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.forms import INVALID_PHONE
from apps.contacts import services as contacts
from apps.contacts.models import Contact
from apps.core import outbound
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display

from .models import CustomerAccount
from .tokens import customer_reset_token, customer_token

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.messaging.recipients import Recipient

logger = logging.getLogger(__name__)


class CustomerError(Exception):
    """A refused customer-account action; the message is safe to show."""


# --- links + mail ---------------------------------------------------------------------

# Each message row and the label on its gold button.
_INVITE = ("customer_invite", "Set your password")
_SIGNUP = ("customer_signup", "Set your password")
_RESET = ("customer_password_reset", "Choose a new password")

SIGNUP_SALT = "customers.signup"
SIGNUP_MAX_AGE = timedelta(days=7)


def _account_link(
    name: str, account: CustomerAccount, generator: PasswordResetTokenGenerator
) -> str:
    path = reverse(
        name,
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": generator.make_token(account),
        },
    )
    return urljoin(settings.PUBLIC_BASE_URL, path)


def _signup_link(name: str, email: str, phone: str) -> str:
    token = signing.dumps({"name": name, "email": email, "phone": phone}, salt=SIGNUP_SALT)
    return urljoin(settings.PUBLIC_BASE_URL, reverse("customers:signup_confirm", args=[token]))


def _mail(message: tuple[str, str], url: str) -> dict:
    """What an account email's row is sent with: the button and the preheader. The greeting
    is the sender's own `first_name`, read off the person it goes to."""
    from apps.messaging import rendering

    key, label = message
    return {
        "blocks": {"account_button": rendering.block("link_button", {"label": label, "url": url})},
        "preheader": rendering.preheader(key, {}),
    }


def _customer(name: str, email: str, contact_id: int | None) -> Recipient:
    from apps.messaging.recipients import Recipient

    return Recipient(
        name=name,
        label=f"{name or email} (customer)",
        phone="",
        email=email,
        contact_id=contact_id,
        channels=("email",),
    )


def _send(message: tuple[str, str], person: Recipient, url: str) -> bool:
    from apps.messaging import sender

    return sender.send_now(message[0], to=[person], **_mail(message, url)).sent


def _send_to_account(message: tuple[str, str], account: CustomerAccount, url: str) -> bool:
    return _send(message, _customer(account.contact.name, account.email, account.contact_id), url)


def _send_invite(account: CustomerAccount) -> bool:
    return _send_to_account(
        _INVITE, account, _account_link("customers:register", account, customer_token)
    )


def _send_reset(account: CustomerAccount) -> bool:
    url = _account_link("customers:set_password", account, customer_reset_token)
    return _send_to_account(_RESET, account, url)


def _assert_outbound() -> None:
    """Refuse a staff send before anything changes: while the go-live switch is off, or
    while PUBLIC_BASE_URL is blank (the link in the mail would be relative, and dead)."""
    if not outbound.enabled():
        raise CustomerError(outbound.OFF_MESSAGE)
    if not settings.PUBLIC_BASE_URL:
        raise CustomerError(
            "PUBLIC_BASE_URL isn't set, so the invite link wouldn't work. "
            "Set it in the app's config, then send the invite."
        )


def _account_for(email: str) -> CustomerAccount | None:
    """The account whose contact has this email, as typed in any case."""
    if not email:
        return None
    return (
        CustomerAccount.objects.select_related("contact")
        .filter(contact__email__iexact=email)
        .first()
    )


# --- staff actions ---------------------------------------------------------------------


def invite(contact: Contact, *, actor: User | None) -> tuple[CustomerAccount, bool]:
    """Give `contact` a pending customer account and email the set-password link.

    A revoked account is re-opened: a fresh invite, a fresh password set by the customer.
    Returns (account, email_sent); the account is kept when delivery fails so staff can
    use Resend.
    """
    _assert_outbound()
    if not (contact.email or "").strip():
        raise CustomerError(f"Add an email address for {contact.name} before inviting them.")
    account = CustomerAccount.objects.filter(contact=contact).first()
    if account is not None and account.status == CustomerAccount.Status.ACTIVE:
        raise CustomerError(f"{contact.name} already has a customer account.")
    if account is not None and account.status == CustomerAccount.Status.INVITED:
        raise CustomerError(f"{contact.name} is already invited. Use Resend to send a new link.")
    account = account or CustomerAccount(contact=contact)
    account.set_unusable_password()
    account.invited_at = timezone.now()
    account.invited_by = actor
    account.registered_at = None
    account.self_registered = False
    account.revoked_at = None
    account.revoked_by = None
    account.save()
    return account, _send_invite(account)


def resend(account: CustomerAccount) -> bool:
    """A new set-password link. The previous one stops working (the token covers
    `invited_at`)."""
    _assert_outbound()
    if account.status != CustomerAccount.Status.INVITED:
        raise CustomerError("Only a pending invite can be resent.")
    account.invited_at = timezone.now()
    account.save(update_fields=["invited_at", "updated_at"])
    return _send_invite(account)


def revoke(account: CustomerAccount, *, actor: User | None) -> None:
    """End access now: the sign-in, every open session and every outstanding link."""
    if account.status == CustomerAccount.Status.REVOKED:
        return
    account.revoked_at = timezone.now()
    account.revoked_by = actor
    account.set_unusable_password()
    account.save(update_fields=["revoked_at", "revoked_by", "password", "updated_at"])


# --- the customer's own actions --------------------------------------------------------


LOCKOUT_FAILURES = 10
LOCKOUT_FOR = timedelta(minutes=15)


def register(account: CustomerAccount, password: str) -> CustomerAccount:
    """Set the password from an invite or reset link. The first time, this is registration.
    Clears any sign-in lock: whoever holds a valid link has proved they own the address."""
    account.set_password(password)
    account.failed_logins = 0
    account.locked_until = None
    fields = ["password", "failed_logins", "locked_until", "updated_at"]
    if account.registered_at is None:
        account.registered_at = timezone.now()
        fields.append("registered_at")
    account.save(update_fields=fields)
    return account


WRONG_CURRENT = "Current password is incorrect."


def change_password(account: CustomerAccount, current: str, new: str) -> None:
    """A signed-in customer's new password, after the current one. Every open session ends
    with it (the session holds a hash of the password), so the caller signs them in again.

    A wrong current password is a failed attempt like a wrong sign-in one: it counts toward
    the lockout, and a locked account is refused with the same sentence. The new password
    meets the customer rules (`forms.password_validators`), judged against the account."""
    from .forms import password_validators  # forms imports the public site; keep it lazy

    now = timezone.now()
    locked = account.locked_until is not None and account.locked_until > now
    if locked or not account.check_password(current):
        if current and not locked:
            _record_failure(account, now)
        raise CustomerError(WRONG_CURRENT)
    try:
        validate_password(new, account, password_validators=password_validators())
    except ValidationError as exc:
        raise CustomerError(" ".join(exc.messages)) from None
    account.set_password(new)
    account.failed_logins, account.locked_until = 0, None
    account.save(update_fields=["password", "failed_logins", "locked_until", "updated_at"])


def update_details(account: CustomerAccount, *, name: str, mobile: str) -> None:
    """A customer's own name and mobile, written the way a staff edit is
    (`contacts.apply_booking_edits`, so the change is audited and the number is validated
    and stored as a contact phone). Email and company are never passed: they are the link
    to the customer's history and billing. A blank mobile leaves the number on file, as it
    does for staff; clearing one is done on the staff contact profile."""
    name, mobile = (name or "").strip(), (mobile or "").strip()
    if not name:
        raise CustomerError("Enter your name.")
    number = to_e164(mobile) if mobile else ""
    if mobile and number is None:
        raise CustomerError(INVALID_PHONE)
    contact = account.contact
    with transaction.atomic():
        contacts.apply_booking_edits(contact, name=name, phone=number or "")
        if number and contact.phone != number:
            # The customer's own number is the one we text; staff's "use for texting".
            contacts.use_for_texting(contact.phones.get(number=number))


def _record_failure(account: CustomerAccount, now: datetime) -> None:
    failures = account.failed_logins + 1
    if failures >= LOCKOUT_FAILURES:
        CustomerAccount.objects.filter(pk=account.pk).update(
            failed_logins=0, locked_until=now + LOCKOUT_FOR
        )
    else:
        CustomerAccount.objects.filter(pk=account.pk).update(failed_logins=failures)


def authenticate(email: str, password: str) -> CustomerAccount | None:
    """The account these credentials open, or None: the same None, in about the same time,
    for an unknown email, a pending invite, a revoked or locked account and a wrong
    password. The hasher runs exactly once on every path, so response time doesn't say
    whether an address has an account. The email is the contact's, read live."""
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    now = timezone.now()
    account = _account_for(email)
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


MAILS_PER_EMAIL = 3  # sign-up and reset mails per address per hour, refused silently past it
MAIL_WINDOW = 60 * 60


def _mail_allowed(email: str) -> bool:
    key = "customer-account-email:" + hashlib.sha256(email.encode()).hexdigest()
    count = cache.get(key, 0)
    cache.set(key, count + 1, MAIL_WINDOW)
    return count < MAILS_PER_EMAIL


def send_password_reset(email: str) -> bool:
    """Email a set-password link to a customer who can sign in. Silent otherwise: the page
    says the same thing either way, so it never confirms who has an account.

    An address with no account still renders the reset's message row, its button and its
    preview line, as `planners.services.send_password_reset` does, so the template work
    matches; the per-address cap keeps the mail provider's own latency from being worth
    probing.
    """
    email = (email or "").strip().lower()
    if not email or not _mail_allowed(email):
        return False
    account = _account_for(email)
    if account is None or not account.can_sign_in:
        _render_reset_for_no_one()
        return False
    if not settings.PUBLIC_BASE_URL:
        logger.warning("customer reset for %s not sent: PUBLIC_BASE_URL is blank", email)
        return False
    return _send_reset(account)


def _render_reset_for_no_one() -> None:
    """Render the reset's message row as a real reset renders it, sending nothing. Never
    raises: the page must answer an address with no account exactly as it answers one with
    an account."""
    from apps.messaging import rendering, sender
    from apps.messaging.models import MessageTemplate, TouchPoint

    try:
        mail = _mail(_RESET, "")
        row = MessageTemplate.objects.get(key=_RESET[0])
        tp = TouchPoint(message=row, kind=row.key)
        context = sender.context(tp, row, _customer("", "", None), {})
        rendering.render_email(row, context, mail["blocks"], preheader=mail["preheader"])
    except Exception:  # noqa: BLE001 - a failed decoy must never show on the page
        logger.exception("customer reset decoy failed to render")


# --- self sign-up ----------------------------------------------------------------------


def _signup_refused() -> CustomerError:
    return CustomerError(
        "We can't set up accounts online right now. "
        f"Please call us at {phone_display(settings.COMPANY_PHONE)}."
    )


def start_signup(*, name: str, email: str, phone: str) -> None:
    """Mail a signed link carrying the sign-up form, and write nothing.

    An email with an active account gets the password-reset email instead; every other
    email gets the sign-up one. Either way the caller hears nothing back, so the form can't
    be used to find out who is a customer. Past three mails an hour to one address it goes
    quiet. Refused (before anything is checked) while the go-live switch is off, or while
    PUBLIC_BASE_URL is blank so the link would be dead.
    """
    if not outbound.enabled() or not settings.PUBLIC_BASE_URL:
        raise _signup_refused()
    name, email, phone = (name or "").strip(), (email or "").strip().lower(), (phone or "").strip()
    if not email or not _mail_allowed(email):
        return
    account = _account_for(email)
    if account is not None and account.status == CustomerAccount.Status.ACTIVE:
        _send_reset(account)
        return
    _send(_SIGNUP, _customer(name, email, None), _signup_link(name, email, phone))


def read_signup(token: str) -> dict:
    """The sign-up form a link carries. Raises `signing.BadSignature` (or its
    `SignatureExpired`) for a tampered or week-old link."""
    return signing.loads(token, salt=SIGNUP_SALT, max_age=SIGNUP_MAX_AGE)


def complete_signup(data: dict, password: str) -> CustomerAccount:
    """Create the account a sign-up link was for, now that its holder has proved the email.

    The contact is found by email alone, in any case, never by phone (family members share
    numbers). A found contact keeps its name; the mobile is added as an extra number when it
    is new. No contact: one is created from the form, with the portal as its channel. A
    pending invite for that contact is completed, not duplicated. An active account means
    the link was already used; a revoked one stays closed.
    """
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    if not email:
        raise CustomerError("This link isn't valid.")
    try:
        with transaction.atomic():
            contact = Contact.objects.find_match(email=email)
            account = None
            if contact is not None:
                account = (
                    CustomerAccount.objects.select_for_update().filter(contact=contact).first()
                )
            if account is not None and account.status == CustomerAccount.Status.ACTIVE:
                raise CustomerError("This link has already been used.")
            if account is not None and account.status == CustomerAccount.Status.REVOKED:
                raise CustomerError(
                    "This account is closed. "
                    f"Please call us at {phone_display(settings.COMPANY_PHONE)} to reopen it."
                )
            if contact is None:
                contact = Contact.objects.create(
                    name=name or email,
                    email=email,
                    phone=to_e164(phone) or phone,
                    channel=Channel.PORTAL,
                )
            elif phone:
                try:
                    contacts.add_phone(contact, phone)
                except contacts.PhoneError:
                    pass  # an undiallable extra number is not worth failing a sign-up over
            account = account or CustomerAccount(contact=contact)
            account.set_password(password)
            account.registered_at = timezone.now()
            account.self_registered = True
            account.failed_logins = 0
            account.locked_until = None
            account.save()
    except IntegrityError:
        # A second submit of the same link, racing the first: it looked before the first
        # one's contact or account existed, and the database stopped its insert.
        winner = _account_for(email)
        if winner is not None and winner.status == CustomerAccount.Status.ACTIVE:
            raise CustomerError("This link has already been used.") from None
        raise
    return account
