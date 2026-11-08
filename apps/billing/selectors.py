"""Read side of billing — what a screen needs to render, in one place.

The customer profile and (Phase 6) the billing screen must never disagree about what a
customer owes, so the figures are computed here once rather than in each view.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.urls import reverse

from . import terms as terms_module
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


def qbo_customer_line(contact) -> dict | None:
    """APC-108: the "QuickBooks customer" line on the Billing card — the billing account's
    Customer when that carries the contact's card receipts, else the contact's own link and its
    state. None while QuickBooks isn't connected, or when the contact has neither a link nor a
    card receipt that goes to QuickBooks (processed on or after the start date)."""
    from . import qbo_receipts
    from .models import CustomerLink
    from .qbo_sync import current_realm

    if not _qbo_connected():
        return None
    link = CustomerLink.objects.filter(contact=contact).first()
    if link is None:
        if qbo_receipts.customer_ref(contact, current_realm()) is not None:
            return {"uses_account": True}
        if not qbo_receipts.carries_receipts(contact):
            return None
        if contact.billing_accounts.exists():
            # The account becomes its customer once the hourly run pushes it here.
            return {"uses_account": True}
    return {
        "uses_account": False,
        "link": link,
        "state": link.qbo_sync_state if link else "not_synced",
        "error": link.qbo_sync_error if link else "",
        "sync_url": reverse("billing_customer_sync", args=[contact.pk]),
    }


def _terms_pickers(account, groups: list) -> dict:
    """The terms pickers' options (APC-96), as plain (value, label) pairs.

    One list for the account modal and one per group: each always carries that record's
    stored value, so a term QuickBooks lacks shows selected instead of the first option. The
    group modal is shared by every group, so each row hands its own list to openGroup(),
    and add-group gets the list with no current value.

    The base list once, then every stored value's label in one batch (`terms.labels`): no
    query per group. Each group also gets `terms_note`, its own terms' label only when it
    reads differently from the account's (`net_30` and "Net 30" read the same).
    """
    base = terms_module.choices()
    account_terms = account.terms if account else ""
    label_for = terms_module.labels([account_terms, *(g.terms for g in groups)])
    account_label = label_for.get(account_terms, "")

    def for_value(current: str) -> list[tuple[str, str]]:
        return terms_module.offer(base, current, label_for.get(current, ""))

    account_options = for_value(account_terms)
    inherit = ("", f"Same as the account — {account_label}") if account else ("", "")
    for group in groups:
        group.term_choices = [inherit, *for_value(group.terms)]
        own = label_for.get(group.terms, "")
        group.terms_note = own if own.lower() != account_label.lower() else ""
    return {
        "billing_terms": account_options,
        "billing_group_terms": [inherit, *base],
        "billing_default_terms": account_terms or _default_terms(account_options),
        "billing_account_terms_label": account_label,
    }


def _default_terms(options: list[tuple[str, str]]) -> str:
    """A new account starts on Net 30: the legacy code, else the company's own "Net 30",
    else its first term — never a value the picker doesn't offer."""
    for value, label in options:
        if value == Terms.NET_30 or label.lower() == Terms.NET_30.label.lower():
            return value
    return options[0][0] if options else ""


def billing_context(contact) -> dict:
    """The Billing card's whole context for one contact.

    The account, then its groups in one prefetch, plus the terms lookups (`_terms_pickers`),
    which do not grow with the groups. A group row reads `account.terms` for the inherit
    case, so the account must come back with the groups rather than each row fetching its
    parent.
    """
    account = BillingAccount.objects.filter(contact=contact).prefetch_related("groups").first()
    groups = list(account.groups.all()) if account else []
    return {
        "billing_account": account,
        "billing_groups": groups,
        # The last group cannot be removed (an account needs one to invoice against), so
        # the row hides the control rather than offering one that can only fail.
        "billing_multiple_groups": len(groups) > 1,
        # The modals' terms pickers. Rendered through components/searchable_select.html,
        # so they want the plain (value, label) pairs.
        **_terms_pickers(account, groups),
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
        "qbo_customer": qbo_customer_line(contact),
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
    ordered = sorted(
        groups, key=lambda g: (contact_ids.index(g.account.contact_id), -g.is_default, g.name)
    )
    # The order's own group stays offered even once neither contact owns it any more (the
    # company's billing contact changed): staff can still change the PO or go back to card.
    if lead.billing_group_id and lead.billing_group_id not in {g.pk for g in ordered}:
        ordered.append(lead.billing_group)
    return ordered


def paid_by_context(lead) -> dict:
    """The money card's "Paid by" line and its picker (APC-105): the groups this order may
    bill to, and the rows the picker draws (built in JS, so as plain data)."""
    from .models import Invoice

    groups = groups_for_lead(lead)
    group = lead.billing_group
    # Every group's label in one batch, not a lookup per group (APC-96).
    labels = terms_module.labels(g.effective_terms for g in [*groups, *([group] if group else [])])
    order_invoices: list = []
    uninvoiced = 0
    if lead.on_account:
        order_invoices = list(
            Invoice.objects.filter(lines__reservation__lead=lead, status=Invoice.Status.OPEN)
            .select_related("group")
            .distinct()
            .order_by("-invoice_date", "-pk")
        )
        uninvoiced = lead.reservations.filter(
            review__processed_at__isnull=False, invoice_line__isnull=True
        ).count()
    return {
        "order_invoices": order_invoices,
        "uninvoiced_processed": uninvoiced,
        "billing_groups": groups,
        "billed_group": group,
        "billed_terms": labels[group.effective_terms] if group else "",
        "billing_picker": [
            {
                "id": g.pk,
                "label": f"{g.account.name} — {g.name}",
                "detail": " · ".join(
                    x
                    for x in (
                        g.invoice_email or "No invoice email yet",
                        labels[g.effective_terms],
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
