"""The contact profile's Customer portal card (spec 2026-11-20 §12): what staff see about a
contact's access. The actions the card posts to are `staff_views`."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import dateformat

from apps.contacts.models import Contact

from . import services
from .models import CustomerAccount

if TYPE_CHECKING:
    from apps.accounts.models import User

_ACCESS_LABELS = {
    "none": "Not invited",
    "invited": "Invited",
    "active": "Active",
    "revoked": "Revoked",
}


def access_for(contact: Contact) -> dict:
    """The card: the state, one line saying who did what when, and which actions apply. No
    query when the contact was loaded with `customer_account__invited_by` and
    `customer_account__revoked_by` selected (the profile does); one otherwise.

    `blocked_reason` is why the card's sending action can't run now, from the same list in
    the same order the action itself refuses by (`services.send_blocked`), so the card and
    the click never name different reasons. The card shows it beside the disabled button.
    Revoke is never blocked. `never_signed_in` is a pending invite: there is no session to
    end, which the revoke confirmation says differently."""
    try:
        account = contact.customer_account
    except CustomerAccount.DoesNotExist:
        account = None
    status = account.status if account else "none"
    zone = ZoneInfo(settings.TIME_ZONE)

    def day(moment: datetime) -> str:
        return dateformat.format(moment.astimezone(zone), "M j, Y")

    def by(user: User | None) -> str:
        return f" by {user}" if user else ""

    if account is None:
        first = (contact.name.split() or ["This contact"])[0]
        line = f"{first} can sign in to request trips, see history and download receipts."
    elif status == CustomerAccount.Status.REVOKED:
        line = f"Access revoked {day(account.revoked_at)}{by(account.revoked_by)}."
    elif status == CustomerAccount.Status.INVITED:
        line = (
            f"Invite emailed {day(account.invited_at)}{by(account.invited_by)}."
            if account.invited_at
            else "Invite pending."
        )
    elif account.self_registered or account.invited_at is None:
        line = f"Signed up on their own {day(account.registered_at)}."
    else:
        line = (
            f"Invited {day(account.invited_at)}{by(account.invited_by)}. "
            f"Signed up {day(account.registered_at)}."
        )
    if status == CustomerAccount.Status.ACTIVE and account.last_login:
        seen = account.last_login.astimezone(zone)
        clock = dateformat.format(seen, "M j \\a\\t g:i A")
        line += f" Last signed in {clock} {seen.tzname()}."

    inviting = status in ("none", CustomerAccount.Status.REVOKED)
    if inviting:
        action = services.INVITE
    elif status == CustomerAccount.Status.INVITED:
        action = services.RESEND
    else:
        action = services.RESET
    blocked = services.send_blocked(contact, action=action)
    return {
        "status": status,
        "label": _ACCESS_LABELS[status],
        "line": line,
        "blocked_reason": blocked,
        "can_invite": inviting and not blocked,
        "show_invite": inviting,
        "show_resend": status == CustomerAccount.Status.INVITED,
        "show_reset": status == CustomerAccount.Status.ACTIVE,
        "can_revoke": status in (CustomerAccount.Status.INVITED, CustomerAccount.Status.ACTIVE),
        "never_signed_in": status == CustomerAccount.Status.INVITED,
    }
