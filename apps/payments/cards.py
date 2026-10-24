"""The customer card vault (APC-47) — every Stripe call for a contact's saved cards.

One Stripe customer per contact (`Contact.stripe_customer_id`). Cards are added through a
SetupIntent the browser confirms with our own card form (`apcPay.cardFields`), then read
back here from Stripe — the browser is never trusted to say what card it saved.

Only brand, last four and expiry are kept. Never store, render or log a PaymentMethod.

Remove is the dangerous one: the balance cron charges `PaymentPlan.stripe_payment_method_id`
30 days before pickup, and charging a detached card fails. So the card is cleared from every
plan that still owes a balance — in the same transaction as the vault row goes — before it is
detached in Stripe. A cleared plan is one the cron skips and the deposit report surfaces.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import PaymentPlan, SavedCard

logger = logging.getLogger(__name__)

# A plan "still owes a balance" while it's scheduled or a charge failed and will be retried.
# Only these are named in the Remove confirm; the card comes off every live plan regardless.
_OWING = (PaymentPlan.BalanceStatus.SCHEDULED, PaymentPlan.BalanceStatus.FAILED)
# Most recently used first, never-used last — on Postgres (prod) as on MySQL (local, test),
# which put NULLs at opposite ends.
_BY_RECENT_USE = (F("last_used_at").desc(nulls_last=True), "-created_at")


def _lead_status():
    from apps.leads.models import Lead

    return Lead.Status


class CardError(Exception):
    """Something the vault can't do; the message is shown to staff."""


def _stripe():
    from .services import _stripe as configured

    return configured()


def contact_customer(contact) -> str:
    """The contact's Stripe customer, created the first time it's needed. Locked and keyed
    so a double-click or two first payments can't make two customers for one person."""
    from apps.contacts.models import Contact

    if contact.stripe_customer_id:
        return contact.stripe_customer_id
    with transaction.atomic():
        locked = Contact.objects.select_for_update().get(pk=contact.pk)
        if not locked.stripe_customer_id:
            customer = _stripe().Customer.create(
                name=locked.name,
                email=locked.email or None,
                phone=locked.phone or None,
                metadata={"contact_id": locked.pk},
                idempotency_key=f"contact-customer-{locked.pk}",
            )
            locked.stripe_customer_id = customer.id
            locked.save(update_fields=["stripe_customer_id", "updated_at"])
    contact.stripe_customer_id = locked.stripe_customer_id
    return contact.stripe_customer_id


def start_add_card(contact) -> str:
    """A SetupIntent on the contact's customer for an off-session card. Returns its client
    secret for `stripe.confirmCardSetup`."""
    intent = _stripe().SetupIntent.create(
        customer=contact_customer(contact),
        payment_method_types=["card"],
        usage="off_session",
        metadata={"contact_id": str(contact.pk)},
    )
    return intent.client_secret


def _record(contact, payment_method, *, used: bool) -> SavedCard:
    """Upsert the vault row for a card attached to the contact's customer. The first card a
    contact has becomes their default."""
    details = getattr(payment_method, "card", None)
    if details is None:
        raise CardError("That payment method isn't a card.")
    from apps.contacts.models import Contact

    with transaction.atomic():
        # Lock the contact itself: locking their cards locks nothing when they have none,
        # and then two first cards could both become the default.
        Contact.objects.select_for_update().filter(pk=contact.pk).first()
        card, created = SavedCard.objects.get_or_create(
            stripe_payment_method_id=payment_method.id,
            defaults={
                "contact": contact,
                "brand": details.brand or "",
                "last4": details.last4,
                "exp_month": details.exp_month,
                "exp_year": details.exp_year,
            },
        )
        if card.contact_id != contact.pk:
            raise CardError("That card is saved for a different customer.")
        fields = []
        if created and not SavedCard.objects.filter(contact=contact, is_default=True).exists():
            card.is_default = True
            fields.append("is_default")
        if used:
            card.last_used_at = timezone.now()
            fields.append("last_used_at")
        if fields:
            card.save(update_fields=[*fields, "updated_at"])
    return card


def finish_add_card(contact, setup_intent_id: str) -> SavedCard:
    """Read the confirmed SetupIntent back from Stripe and record its card. Refuses one that
    isn't finished or belongs to someone else's customer."""
    intent = _stripe().SetupIntent.retrieve(setup_intent_id, expand=["payment_method"])
    if intent.status != "succeeded":
        raise CardError("The card wasn't confirmed. Please try again.")
    if not contact.stripe_customer_id or intent.customer != contact.stripe_customer_id:
        raise CardError("That card belongs to a different customer.")
    return _record(contact, intent.payment_method, used=False)


def remember_card(plan: PaymentPlan, payment_method) -> None:
    """A payment on `plan` saved a card. When the plan is on its contact's own Stripe
    customer and the card is attached to it, the card is the contact's — put it in the vault.
    Anything else is left alone: a legacy order's card, and a detached card coming back on a
    late webhook or a held deposit's capture (it has no customer).

    Bookkeeping only: the payment has already succeeded, so nothing here may break it."""
    contact = plan.lead.contact
    customer = getattr(payment_method, "customer", None)
    if not contact.stripe_customer_id or plan.stripe_customer_id != contact.stripe_customer_id:
        return
    if customer != contact.stripe_customer_id:
        return
    try:
        _record(contact, payment_method, used=True)
    except Exception:
        logger.exception("saving a paid card to the vault failed (plan %s)", plan.pk)


def make_default(card: SavedCard) -> SavedCard:
    """`card` becomes the contact's only default. A vault preference only: an order holds a
    card once the customer pays on it, never because it is the default (Moe, 2026-10-24)."""
    from apps.contacts.models import Contact

    with transaction.atomic():
        Contact.objects.select_for_update().filter(pk=card.contact_id).first()
        rows = SavedCard.objects.filter(contact_id=card.contact_id)
        rows.exclude(pk=card.pk).update(is_default=False)
        rows.filter(pk=card.pk).update(is_default=True)
    card.refresh_from_db()
    return card


def plans_holding(card: SavedCard) -> list[PaymentPlan]:
    """Orders that would charge this card for a balance they still owe."""
    return list(
        PaymentPlan.objects.filter(
            stripe_payment_method_id=card.stripe_payment_method_id,
            balance_status__in=_OWING,
            lead__status=_lead_status().BOOKED,
        )
        .select_related("lead")
        .order_by("lead__pk")
    )


def remove_card(card: SavedCard) -> list:
    """Remove the card. It comes off every live order that holds it, in the same
    transaction as the vault row goes — not only the ones that still owe a balance: a paid
    order can still charge its card for the final bill, and an engaged order's held deposit
    would put it back on capture. So nothing can charge it after this (the balance cron
    skips a card-less plan; the deposit report surfaces it). Then it is detached in Stripe.
    Past charges keep the card they were paid with. Returns the leads that still owed a
    balance on it — the ones the confirm named."""
    from apps.contacts.models import Contact

    with transaction.atomic():
        Contact.objects.select_for_update().filter(pk=card.contact_id).first()
        card = SavedCard.objects.get(pk=card.pk)  # re-read under the lock
        holding = plans_holding(card)
        live = PaymentPlan.objects.filter(
            stripe_payment_method_id=card.stripe_payment_method_id
        ).exclude(lead__status=_lead_status().LOST)
        live.update(
            stripe_payment_method_id="", card_brand="", card_last4="", updated_at=timezone.now()
        )
        was_default = card.is_default
        contact_id = card.contact_id
        pm_id = card.stripe_payment_method_id
        card.delete()
        if was_default:
            nxt = SavedCard.objects.filter(contact_id=contact_id).order_by(*_BY_RECENT_USE).first()
            if nxt is not None:
                SavedCard.objects.filter(pk=nxt.pk).update(is_default=True)
    try:
        _stripe().PaymentMethod.detach(pm_id)
    except Exception:
        # The vault and the orders are already safe; Stripe still holding the card can
        # only mean it's never charged again from here. Logged for the office, not raised.
        logger.exception("detaching a removed card from Stripe failed (contact %s)", contact_id)
    return [p.lead for p in holding]
