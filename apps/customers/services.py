"""Every customer-account write: invite, self sign-up, sign-in, password reset and change.

Mirrors `apps/planners/services.py`. The three account emails (`customer_invite`,
`customer_signup`, `customer_password_reset`) are message rows sent through the shared
sender as customer mail, so the go-live switch (APC-90) gates them like any other send.
Staff actions are refused outright while the switch is off, with the same sentence every
other agent send uses, rather than creating an account nobody was told about.

Self sign-up creates no contact and no account until the email is proved: `start_signup`
only mails a signed link carrying the form (the mail itself is logged like any other send),
and `complete_signup` creates or attaches the account when the link is used. Nothing either
returns or raises says whether an email already has an account.

This module also holds the rules the forms share: what a customer's name, mobile and
password must be (`clean_name`, `clean_mobile`, `password_validators`).
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
    validate_password,
)
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
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
from apps.planners.selectors import dispatch_phone

from . import throttle
from .models import CustomerAccount
from .tokens import customer_reset_token, customer_token

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.messaging.recipients import Recipient

logger = logging.getLogger(__name__)


class CustomerError(Exception):
    """A refused customer-account action; the message is safe to show. `field` names the
    form field the refusal is about, when it is about one."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# --- what a name, a mobile and a password must be ---------------------------------------

# The set-password screen promises "At least 10 characters."; the site-wide minimum is 8.
MIN_PASSWORD_LENGTH = 10


def password_validators() -> list:
    """The site's password validators, with the customer screen's longer minimum."""
    rest = [
        v for v in get_default_password_validators() if not isinstance(v, MinimumLengthValidator)
    ]
    return [MinimumLengthValidator(MIN_PASSWORD_LENGTH), *rest]


def clean_name(raw: str | None) -> str:
    """A name as typed, made safe to store and print: control characters (a pasted line
    break, a tab, an escape) become spaces, and runs of spaces become one."""
    text = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in raw or "")
    return " ".join(text.split())


def clean_mobile(raw: str | None) -> str:
    """A mobile as typed, in E.164, or "" when none was typed. Raises `CustomerError` for a
    number that can't be dialled."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    number = to_e164(raw)
    if number is None:
        raise CustomerError(INVALID_PHONE, field="mobile")
    return number


def _call_us() -> str:
    """The number every portal page shows (`planners.selectors.dispatch_phone`)."""
    return dispatch_phone()["display"]


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

    # `{company_phone}` is the dispatch number the portal's own pages show, formatted.
    extra = {"company_phone": _call_us()}
    return sender.send_now(message[0], to=[person], extra_ctx=extra, **_mail(message, url)).sent


def _send_to_account(message: tuple[str, str], account: CustomerAccount, url: str) -> bool:
    return _send(message, _customer(account.contact.name, account.email, account.contact_id), url)


def _send_invite(account: CustomerAccount) -> bool:
    return _send_to_account(
        _INVITE, account, _account_link("customers:register", account, customer_token)
    )


def _send_reset(account: CustomerAccount) -> bool:
    url = _account_link("customers:set_password", account, customer_reset_token)
    return _send_to_account(_RESET, account, url)


def _assert_can_send(contact: Contact, action: str) -> None:
    """Refuse a staff send before anything changes, with the reason the card shows."""
    reason = send_blocked(contact, action=action)
    if reason:
        raise CustomerError(reason)


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


AFFILIATE_REFUSED = "Portal access is not available for affiliate companies."
NOT_ACTIVE_FOR_RESET = "Only a customer who has signed up can get a password reset."
NO_BASE_URL = (
    "PUBLIC_BASE_URL isn't set, so the link in the email wouldn't work. "
    "Set it in the app's config, then try again."
)
INVITE, RESEND, RESET = "invite", "resend", "reset"


def is_affiliate(contact: Contact) -> bool:
    """Whether the contact books for an affiliate company: its portal would show no trips
    (`selectors.visible_trips` leaves farm-ins out) and could not request one."""
    company = contact.company
    return company is not None and company.is_affiliate_account


def no_email_message(contact: Contact, *, inviting: bool = True) -> str:
    if inviting:
        return f"Add an email address for {contact.name} before inviting them."
    return f"Add an email address for {contact.name} first."


def send_blocked(contact: Contact, *, action: str) -> str:
    """Why staff can't send this contact an account email now (`action` is `INVITE`,
    `RESEND` or `RESET`), or "". One list in one order, read by the contact profile's card
    and by the actions themselves, so the card never names a different reason than the
    click gets: an affiliate company (an invite or a resend only: an account that already
    exists may still be sent a reset, and revoked), no email on the contact, the go-live
    switch off, then PUBLIC_BASE_URL blank (the link in the mail would be dead)."""
    if action != RESET and is_affiliate(contact):
        return AFFILIATE_REFUSED
    if not (contact.email or "").strip():
        return no_email_message(contact, inviting=action == INVITE)
    if not outbound.enabled():
        return outbound.OFF_MESSAGE
    if not settings.PUBLIC_BASE_URL:
        return NO_BASE_URL
    return ""


def invite(contact: Contact, *, actor: User | None) -> tuple[CustomerAccount, bool]:
    """Give `contact` a pending customer account and email the set-password link.

    A revoked account is re-opened: a fresh invite, a fresh password set by the customer.
    Refused for a contact of an affiliate company. Returns (account, email_sent); the
    account is kept when delivery fails so staff can use Resend.
    """
    _assert_can_send(contact, INVITE)
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
    `invited_at`). Refused, like a first invite, once the contact's company is an
    affiliate."""
    if account.status != CustomerAccount.Status.INVITED:
        raise CustomerError("Only a pending invite can be resent.")
    _assert_can_send(account.contact, RESEND)
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


def staff_password_reset(account: CustomerAccount) -> bool:
    """A reset link sent by staff from the contact profile: the customer's own reset
    email, under the same hourly cap per address. Refused, with the reason, before the
    customer has signed up, while a send is blocked (`send_blocked`), or past the cap.
    Returns whether the email went: False only when delivery itself failed, so staff are
    never told one went when it did not, nor told the cap is why when it is not."""
    if account.status != CustomerAccount.Status.ACTIVE:
        raise CustomerError(NOT_ACTIVE_FOR_RESET)
    _assert_can_send(account.contact, RESET)
    if not _mail_allowed(account.email):
        raise CustomerError(RESET_CAPPED)
    return _send_reset(account)


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
    meets the customer rules (`password_validators`), judged against the account. Each
    refusal names its field: the current password, or the new one."""
    now = timezone.now()
    locked = account.locked_until is not None and account.locked_until > now
    if locked or not account.check_password(current):
        if current and not locked:
            _record_failure(account, now)
        raise CustomerError(WRONG_CURRENT, field="current_password")
    try:
        validate_password(new, account, password_validators=password_validators())
    except ValidationError as exc:
        raise CustomerError(" ".join(exc.messages), field="new_password1") from None
    account.set_password(new)
    account.failed_logins, account.locked_until = 0, None
    account.save(update_fields=["password", "failed_logins", "locked_until", "updated_at"])


def update_details(account: CustomerAccount, *, name: str, mobile: str) -> None:
    """A customer's own name and mobile, written the way a staff edit is
    (`contacts.apply_booking_edits`, so the change is audited and the number is validated
    and stored as a contact phone). Email and company are never passed: they are the link
    to the customer's history and billing. A blank mobile leaves the number on file, as it
    does for staff; clearing one is done on the staff contact profile. So a number already
    on file that can't be dialled (old, imported data) never stands in the way of a name
    edit: the Account form sends a mobile only when the customer changed it."""
    name = clean_name(name)
    if not name:
        raise CustomerError("Enter your name.", field="name")
    number = clean_mobile(mobile)
    contact = account.contact
    with transaction.atomic():
        contacts.apply_booking_edits(contact, name=name, phone=number or "")
        if number and contact.phone != number:
            # The customer's own number is the one we text; staff's "use for texting".
            contacts.use_for_texting(contact.phones.get(number=number))


def _record_failure(account: CustomerAccount, now: datetime) -> None:
    """Count one wrong password, and lock the account at the tenth. The count is read
    under a row lock, not off `account`: wrong guesses sent side by side each hold a copy
    read before the others were counted, and would otherwise all write the same number."""
    with transaction.atomic():
        row = CustomerAccount.objects.select_for_update().get(pk=account.pk)
        row.failed_logins += 1
        if row.failed_logins >= LOCKOUT_FAILURES:
            row.failed_logins, row.locked_until = 0, now + LOCKOUT_FOR
        row.save(update_fields=["failed_logins", "locked_until"])
    account.failed_logins, account.locked_until = row.failed_logins, row.locked_until


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
MAIL_WINDOW = timedelta(hours=1)
RESET_CAPPED = (
    f"One address gets at most {MAILS_PER_EMAIL} sign-up or reset emails an hour. Try again later."
)


def _mail_allowed(email: str) -> bool:
    """Count one more account mail to `email`; False past `MAILS_PER_EMAIL` in the hour.
    Counted in the database (`throttle`), so the cap is the same on every web worker."""
    return throttle.hit("account-email", email, MAIL_WINDOW) <= MAILS_PER_EMAIL


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
        f"We can't set up accounts online right now. Please call us at {_call_us()}."
    )


USED_LINK = "This link has already been used."


def _closed() -> CustomerError:
    return CustomerError(f"This account is closed. Please call us at {_call_us()} to reopen it.")


def _refuse_finished(account: CustomerAccount | None) -> None:
    """A sign-up link is spent once its email has an active account, and stays closed for
    a revoked one."""
    if account is None:
        return
    if account.status == CustomerAccount.Status.ACTIVE:
        raise CustomerError(USED_LINK)
    if account.status == CustomerAccount.Status.REVOKED:
        raise _closed()


def start_signup(*, name: str, email: str, phone: str) -> None:
    """Mail a signed link carrying the sign-up form. No contact and no account is created;
    the mail leaves the same trail as every other send (a TouchPoint and a Delivery holding
    the typed address), and the hourly cap counts it.

    An email with an active account gets the password-reset email instead; every other
    email gets the sign-up one. Either way the caller hears nothing back, so the form can't
    be used to find out who is a customer. Past three mails an hour to one address it goes
    quiet. Refused (before anything is checked) while the go-live switch is off, or while
    PUBLIC_BASE_URL is blank so the link would be dead.
    """
    if not outbound.enabled() or not settings.PUBLIC_BASE_URL:
        raise _signup_refused()
    name, email, phone = clean_name(name), (email or "").strip().lower(), (phone or "").strip()
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


def check_signup(data: dict) -> None:
    """Refuse a sign-up link that can no longer make an account, before its holder is
    asked for a password: raises `CustomerError` with why (already used, or the account
    was closed). The holder proved the address by opening the link, so saying so tells
    them nothing they could not already see."""
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise CustomerError("This link isn't valid.")
    _refuse_finished(_account_for(email))


def signup_stand_in(data: dict) -> CustomerAccount:
    """An unsaved account carrying a sign-up link's email, for the password rules to judge
    the new password against (a password that is the email address is refused, as it is on
    every other screen)."""
    return CustomerAccount(contact=Contact(name=data.get("name") or "", email=data.get("email")))


def complete_signup(data: dict, password: str) -> CustomerAccount:
    """Create the account a sign-up link was for, now that its holder has proved the email.

    The contact is found by email alone, in any case, never by phone (family members share
    numbers). A found contact keeps its name; the mobile is added as an extra number when it
    is new. No contact: one is created from the form, with the portal as its channel. A
    pending invite for that contact is completed, not duplicated. An active account means
    the link was already used; a revoked one stays closed.
    """
    name = clean_name(data.get("name"))
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
            _refuse_finished(account)
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
            raise CustomerError(USED_LINK) from None
        raise
    return account
