"""Every billing-account change goes through here — views never touch the models directly.

Two rules carry the design, and both live in code rather than in the database:

* **An account always has at least one group.** Enforced by making the first group part of
  creating the account, in one transaction. There is no window in which an account exists
  with nothing to invoice against, so nothing has to validate for one later.
* **Exactly one default group per account.** A conditional `UniqueConstraint` would say
  this, and that is the trap `dispatch/services.py` documents: MySQL (local and test) has
  no partial indexes, so the constraint would exist on prod Postgres and silently not where
  the tests run. It is enforced here instead, under `select_for_update()`.
"""

from __future__ import annotations

from django.db import transaction

from .models import AccountGroup, BillingAccount, Terms


class BillingError(ValueError):
    """A billing rule refused the change. Surfaced to staff, never a 500."""


def _required(value: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise BillingError(f"{what} is required.")
    return text


@transaction.atomic
def create_account(
    contact,
    *,
    name: str,
    terms: str = Terms.NET_30,
    group_name: str,
    invoice_email: str = "",
    po_required: bool = False,
) -> BillingAccount:
    """Open a terms account for a contact, together with the first group to invoice.

    The group is not optional and not a second step: an account with no group cannot be
    invoiced, so the two are created together or neither is.
    """
    name = _required(name, "An account name")
    group_name = _required(group_name, "A group name")
    if BillingAccount.objects.filter(contact=contact).exists():
        raise BillingError(f"{contact.name} already has a billing account.")

    account = BillingAccount.objects.create(contact=contact, name=name, terms=terms)
    AccountGroup.objects.create(
        account=account,
        name=group_name,
        invoice_email=invoice_email.strip(),
        po_required=po_required,
        is_default=True,  # the only group, so it is the one orders bill to
    )
    return account


@transaction.atomic
def add_group(
    account: BillingAccount,
    *,
    name: str,
    invoice_email: str = "",
    terms: str = "",
    po_required: bool = False,
) -> AccountGroup:
    """Another department to invoice under this account. Never the default — that is a
    deliberate, separate choice (`set_default_group`)."""
    name = _required(name, "A group name")
    if account.groups.filter(name=name).exists():
        raise BillingError(f"{account.name} already has a group called “{name}”.")
    return AccountGroup.objects.create(
        account=account,
        name=name,
        invoice_email=invoice_email.strip(),
        terms=terms,
        po_required=po_required,
    )


@transaction.atomic
def update_account(account: BillingAccount, *, name: str, terms: str) -> BillingAccount:
    """Rename the account or move its terms.

    The name is the customer's name in QuickBooks, so a rename here is a rename there —
    which is why Phase 2 marks the record for re-sync rather than assuming the two agree.
    """
    account.name = _required(name, "An account name")
    account.terms = terms
    account.save(update_fields=["name", "terms", "updated_at"])
    return account


@transaction.atomic
def update_group(
    group: AccountGroup,
    *,
    name: str,
    invoice_email: str = "",
    terms: str = "",
    po_required: bool = False,
) -> AccountGroup:
    """Edit a group in place. Never touches `is_default` — that is `set_default_group`."""
    name = _required(name, "A group name")
    clash = group.account.groups.filter(name=name).exclude(pk=group.pk)
    if clash.exists():
        raise BillingError(f"{group.account.name} already has a group called “{name}”.")
    group.name = name
    group.invoice_email = invoice_email.strip()
    group.terms = terms
    group.po_required = po_required
    group.save(update_fields=["name", "invoice_email", "terms", "po_required", "updated_at"])
    return group


@transaction.atomic
def set_default_group(group: AccountGroup) -> AccountGroup:
    """Make this the group orders bill to, and the only one.

    Locks the account first: two staff setting a different default at once would otherwise
    both clear the old one and both set theirs, leaving two.
    """
    BillingAccount.objects.select_for_update().get(pk=group.account_id)
    AccountGroup.objects.filter(account_id=group.account_id).exclude(pk=group.pk).update(
        is_default=False
    )
    if not group.is_default:
        group.is_default = True
        group.save(update_fields=["is_default", "updated_at"])
    return group


@transaction.atomic
def delete_group(group: AccountGroup) -> None:
    """Remove a group, unless doing so would leave the account unable to be invoiced.

    Refusing to delete the default — rather than quietly promoting another — keeps the
    choice with the person: which department picks up this account's orders is theirs to
    decide, not ours to guess.
    """
    BillingAccount.objects.select_for_update().get(pk=group.account_id)
    siblings = AccountGroup.objects.filter(account_id=group.account_id).exclude(pk=group.pk)
    if not siblings.exists():
        raise BillingError(
            "This is the account's last group — an account needs one to invoice against."
        )
    if group.is_default:
        raise BillingError("Make another group the default before removing this one.")
    group.delete()
