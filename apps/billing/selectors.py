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


def cards_for(contact) -> list[dict]:
    """Every distinct card this customer has paid with, most recently used first.

    There is no card store on a contact — `PaymentPlan` is one per order and holds the card
    that order used — so this is derived, which is also why the tab is display-only until
    the vault exists (APC-47).

    Two sources, because neither alone is complete:

    * `Charge` (APC-40) is what actually paid: immutable, dated, and it keeps cards that a
      later swap would otherwise erase from the plan.
    * `PaymentPlan` is the card currently on file. It covers charges taken before APC-40
      recorded one, and a card saved on an order that has not been charged yet.

    Charges are read first and win the date, because "last used" means money moved. A
    plan's `updated_at` only says when the row was last touched, so it is the fallback for
    a card that has no charge to date it.

    Two queries whatever the customer's history, both `select_related` down to the lead.
    """
    from apps.payments.models import Charge, PaymentPlan

    found: dict[tuple[str, str], dict] = {}

    def remember(brand: str, last4: str, lead, used_at, *, used: bool, customer="") -> None:
        if not last4:
            return
        # First writer wins: charges are iterated first, newest first.
        found.setdefault(
            (brand, last4),
            {
                "brand": brand,
                "last4": last4,
                "lead": lead,
                "used_at": used_at,
                "used": used,
                "customer": customer,
            },
        )

    charges = (
        Charge.objects.filter(
            plan__lead__contact=contact,
            status__in=(Charge.Status.SUCCEEDED, Charge.Status.AUTHORIZED),
        )
        .exclude(card_last4="")
        .select_related("plan__lead")
        .order_by("-created_at")
    )
    for charge in charges:
        remember(
            charge.card_brand,
            charge.card_last4,
            charge.plan.lead,
            charge.created_at,
            used=True,
            customer=charge.plan.stripe_customer_id,
        )

    plans = (
        PaymentPlan.objects.filter(lead__contact=contact)
        .exclude(card_last4="")
        .select_related("lead")
        .order_by("-updated_at")
    )
    for plan in plans:
        # `used=False`: a card can be saved on a plan without anything ever being charged
        # to it, and the row must not claim otherwise. A card that *has* been charged was
        # already recorded above with its real date, so this never downgrades one.
        remember(
            plan.card_brand,
            plan.card_last4,
            plan.lead,
            plan.updated_at,
            used=False,
            customer=plan.stripe_customer_id,
        )

    return sorted(found.values(), key=lambda card: card["used_at"], reverse=True)


def vault_cards(contact) -> list:
    """The contact's saved cards (APC-47), each with `open_orders` — the quote numbers that
    would lose the card on Remove because they still owe a balance on it. Two queries."""
    from apps.leads.models import Lead
    from apps.payments.cards import _OWING
    from apps.payments.models import PaymentPlan, SavedCard

    saved = list(SavedCard.objects.filter(contact=contact))
    if not saved:
        return []
    holding: dict[str, list[str]] = {}
    plans = PaymentPlan.objects.filter(
        stripe_payment_method_id__in=[c.stripe_payment_method_id for c in saved],
        balance_status__in=_OWING,
        lead__status=Lead.Status.BOOKED,
    ).select_related("lead")
    for plan in plans.order_by("lead__pk"):
        holding.setdefault(plan.stripe_payment_method_id, []).append(plan.lead.quote_no)
    for card in saved:
        card.open_orders = holding.get(card.stripe_payment_method_id, [])
    return saved


def _qbo_connected() -> bool:
    from apps.integrations import qbo

    return qbo.is_connected()


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
        # APC-47: the vault, then — read-only — cards from orders that were paid on their
        # own Stripe customer before the vault existed (they can't be moved over).
        **_card_context(contact),
        # Blank client id = QuickBooks was never connected, so every record is NOT_SYNCED
        # and a sync chip on each row would be a wall of red saying nothing. Phase 2 turns
        # this on and the chips appear with no template change.
        # A live connection (APC-41), not just keys in settings — a developer with keys in
        # .env and nothing connected must not see a wall of red chips.
        "qbo_connected": _qbo_connected(),
    }


def _card_context(contact) -> dict:

    vault = vault_cards(contact)
    # "From earlier orders" is only what an order paid on its own (legacy) Stripe customer —
    # a card on the contact's own customer is a vault card, or one that was removed.
    own = contact.stripe_customer_id
    legacy = [c for c in cards_for(contact) if not own or c["customer"] != own]
    return {
        "billing_vault_cards": vault,
        "billing_cards": legacy,
        "billing_has_any_card": bool(vault or legacy),
        "billing_card_count": len(vault) + len(legacy),
        "stripe_pk": settings.STRIPE_PUBLISHABLE_KEY,
    }


def groups_for_lead(lead) -> list:
    """The groups an order may be billed to (APC-105): those under the booking contact's
    account, then those under the order's billing contact's account when that is someone
    else — a colleague booking for a company whose account sits on its billing contact."""
    from .models import AccountGroup

    contact_ids = list(dict.fromkeys([lead.contact_id, lead.effective_billing_contact.pk]))
    groups = AccountGroup.objects.filter(account__contact_id__in=contact_ids).select_related(
        "account"
    )
    return sorted(
        groups, key=lambda g: (contact_ids.index(g.account.contact_id), -g.is_default, g.name)
    )


def _terms_label(code: str) -> str:
    return dict(Terms.choices).get(code, code)


def paid_by_context(lead) -> dict:
    """The money card's "Paid by" line and its picker (APC-105): the groups this order may
    bill to, and the rows the picker draws (built in JS, so as plain data)."""
    groups = groups_for_lead(lead)
    group = lead.billing_group
    return {
        "billing_groups": groups,
        "billed_group": group,
        "billed_terms": _terms_label(group.effective_terms) if group else "",
        "billing_picker": [
            {
                "id": g.pk,
                "label": f"{g.account.name} — {g.name}",
                "detail": " · ".join(
                    x
                    for x in (
                        g.invoice_email or "No invoice email yet",
                        _terms_label(g.effective_terms),
                        "PO number required" if g.po_required else "",
                    )
                    if x
                ),
                "po": g.po_required,
                "default": g.is_default,
            }
            for g in groups
        ],
    }
