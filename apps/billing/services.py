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

from .models import AccountGroup, BillingAccount, SyncState, Terms


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
    account.qbo_sync_state = SyncState.NOT_SYNCED  # the QuickBooks customer needs the change
    account.save(update_fields=["name", "terms", "qbo_sync_state", "updated_at"])
    # Groups may inherit the account's terms, so they go out again too (a harmless sparse
    # update when nothing of theirs changed).
    account.groups.update(qbo_sync_state=SyncState.NOT_SYNCED)
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
    group.qbo_sync_state = SyncState.NOT_SYNCED  # the QuickBooks sub-customer needs the change
    group.save(
        update_fields=[
            "name",
            "invoice_email",
            "terms",
            "po_required",
            "qbo_sync_state",
            "updated_at",
        ]
    )
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
    orders = group.orders.count()
    if orders:
        raise BillingError(
            f"{orders} order{'s' if orders != 1 else ''} bill{'s' if orders == 1 else ''} "
            "to this group. Move them first."
        )
    group.delete()


# --- an order billed to a group (APC-105) ------------------------------------------------


def _locked_lead(lead):
    from apps.leads.models import Lead

    return Lead.objects.select_for_update().select_related("contact__company").get(pk=lead.pk)


@transaction.atomic
def bill_to_account(lead, group: AccountGroup, *, po_number: str, user) -> object:
    """Invoice the whole order to `group` instead of a card (spec 2026-10-25 §5).

    Only a label moves; the ledger does not. Refused once the card holds the customer's
    money — collected, or a deposit on hold — because a trip's price would then be part
    card, part invoice; the agent refunds or releases it first.
    """
    from apps.leads.models import Lead
    from apps.payments import ledger
    from apps.payments import services as payments
    from apps.reservations.models import TripReview

    from . import selectors

    lead = _locked_lead(lead)
    if lead.status == Lead.Status.LOST:
        raise BillingError("A lost order can't be billed to an account.")
    if lead.status == Lead.Status.ENGAGED:
        raise BillingError(
            "The customer's deposit is on hold on their card. Confirm or cancel it first."
        )
    if group.pk not in {g.pk for g in selectors.groups_for_lead(lead)}:
        raise BillingError(f"{group.name} isn't one of this customer's billing groups.")
    po_number = (po_number or "").strip()
    if group.po_required and not po_number:
        raise BillingError(f"{group.name} needs a PO number on every order.")
    if ledger.order_balances(lead)["collected"] > 0:
        raise BillingError("Refund the card payments first, then bill to account.")
    if TripReview.objects.filter(reservation__lead=lead, processed_at__isnull=False).exists():
        # Settled under card rules (a pay link may be open on it); it would be billed twice.
        raise BillingError("Unlock its processed trips first, then bill to account.")
    try:
        payments.close_open_intents(lead)
    except payments.PaymentError as exc:
        raise BillingError(str(exc)) from exc
    lead.billing_group, lead.po_number = group, po_number
    lead.save(update_fields=["billing_group", "po_number", "updated_at"])
    transaction.on_commit(lambda: _resync_tasks(lead))
    return lead


@transaction.atomic
def bill_to_card(lead, *, user) -> object:
    """Back to card: the group and PO are cleared and the card paths apply again.

    Refused once a trip was processed on account: it was locked with its price owed to the
    invoice, and would read as owed to a card nobody charged. (APC-106 adds the invoice
    lines to this check.)
    """
    from apps.reservations.models import TripReview

    lead = _locked_lead(lead)
    if TripReview.objects.filter(reservation__lead=lead, processed_at__isnull=False).exists():
        raise BillingError("Unlock its processed trips first, then switch it back to card.")
    lead.billing_group, lead.po_number = None, ""
    lead.save(update_fields=["billing_group", "po_number", "updated_at"])
    transaction.on_commit(lambda: _resync_tasks(lead, reopen=True))
    return lead


def _resync_tasks(lead, *, reopen: bool = False) -> None:
    """The deposit and balance tasks follow how the order is paid."""
    from apps.tasks import services as tasks

    if reopen:
        tasks.reopen_card_tasks(lead)
    tasks.sync(lead)
