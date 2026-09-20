"""Read side of billing — what a screen needs to render, in one place.

The customer profile and (Phase 6) the billing screen must never disagree about what a
customer owes, so the figures are computed here once rather than in each view.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings

from .models import BillingAccount, Terms

ZERO = Decimal("0.00")


def account_figures(account: BillingAccount | None) -> dict:
    """What the account block leads with: owed, not yet billed, and how overdue.

    Every figure is zero until Phase 4, because nothing can be invoiced yet — but the
    shape is real and the card already reads it. That is the difference between Phase 4
    filling in four numbers and Phase 4 rebuilding the card.

    `oldest_days` is None rather than 0 for "nothing outstanding": zero days would read as
    an invoice raised today, which is not the same thing.
    """
    return {
        "open_balance": ZERO,
        "uninvoiced_total": ZERO,
        "uninvoiced_orders": 0,
        "oldest_days": None,
    }


def billing_context(contact) -> dict:
    """The Billing card's whole context for one contact.

    Two queries whatever the account holds — the account, then its groups in one prefetch.
    A group row reads `account.terms` for the inherit case, so the account must come back
    with the groups rather than each row fetching its parent.
    """
    account = BillingAccount.objects.filter(contact=contact).prefetch_related("groups").first()
    groups = list(account.groups.all()) if account else []
    return {
        "billing_account": account,
        "billing_groups": groups,
        # The last group cannot be removed (an account needs one to invoice against), so
        # the row hides the control rather than offering one that can only fail.
        "billing_multiple_groups": len(groups) > 1,
        # The modals' terms picker. Rendered through components/searchable_select.html,
        # so it wants the plain (value, label) pairs.
        "billing_terms": Terms.choices,
        "account_figures": account_figures(account) if account else None,
        # Filled in by APC-39, which reads the cards off this contact's payment plans.
        "billing_cards": [],
        # Blank client id = QuickBooks was never connected, so every record is NOT_SYNCED
        # and a sync chip on each row would be a wall of red saying nothing. Phase 2 turns
        # this on and the chips appear with no template change.
        "qbo_connected": bool(settings.QBO_CLIENT_ID),
    }
