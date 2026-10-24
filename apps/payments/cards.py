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
from django.utils import timezone

from .models import PaymentPlan, SavedCard

logger = logging.getLogger(__name__)

# A plan "still owes a balance" while it's scheduled or a charge failed and will be retried.
_OWING = (PaymentPlan.BalanceStatus.SCHEDULED, PaymentPlan.BalanceStatus.FAILED)


class CardError(Exception):
    """Something the vault can't do; the message is shown to staff."""


def _stripe():
    from .services import _stripe as configured

    return configured()


def contact_customer(contact) -> str:
    """The contact's Stripe customer, created the first time it's needed."""
    if contact.stripe_customer_id:
        return contact.stripe_customer_id
    customer = _stripe().Customer.create(
        name=contact.name,
        email=contact.email or None,
        phone=contact.phone or None,
        metadata={"contact_id": contact.pk},
    )
    contact.stripe_customer_id = customer.id
    contact.save(update_fields=["stripe_customer_id", "updated_at"])
    return customer.id


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
    with transaction.atomic():
        # Lock the contact's cards so two first cards can't both become the default.
        list(SavedCard.objects.select_for_update().filter(contact=contact).values_list("pk"))
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
    customer the card is the contact's — put it in the vault. A plan on its own legacy
    customer is left alone: that card can't be charged on any other order."""
    contact = plan.lead.contact
    customer = getattr(payment_method, "customer", None)
    if not contact.stripe_customer_id or plan.stripe_customer_id != contact.stripe_customer_id:
        return
    if customer and customer != contact.stripe_customer_id:
        return
    try:
        _record(contact, payment_method, used=True)
    except CardError:
        pass


def adopt_default_card(plan: PaymentPlan) -> None:
    """A new order on the contact's customer starts with the contact's default card."""
    contact = plan.lead.contact
    if plan.stripe_payment_method_id or plan.stripe_customer_id != contact.stripe_customer_id:
        return
    card = SavedCard.objects.filter(contact=contact, is_default=True).first()
    if card is None:
        return
    plan.stripe_payment_method_id = card.stripe_payment_method_id
    plan.card_brand = card.brand
    plan.card_last4 = card.last4
    plan.save(update_fields=["stripe_payment_method_id", "card_brand", "card_last4", "updated_at"])


def make_default(card: SavedCard) -> SavedCard:
    """`card` becomes the contact's only default."""
    with transaction.atomic():
        rows = SavedCard.objects.select_for_update().filter(contact_id=card.contact_id)
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
            lead__status="booked",
        )
        .select_related("lead")
        .order_by("lead__pk")
    )


def remove_card(card: SavedCard) -> list:
    """Remove the card. Every order that still owes a balance on it loses the card in the
    same transaction (so the balance cron skips it rather than charging a detached card);
    then it is detached in Stripe. Past charges keep the card they were paid with. Returns
    the leads whose card was cleared."""
    with transaction.atomic():
        holding = plans_holding(card)
        PaymentPlan.objects.filter(pk__in=[p.pk for p in holding]).update(
            stripe_payment_method_id="", card_brand="", card_last4="", updated_at=timezone.now()
        )
        was_default = card.is_default
        contact_id = card.contact_id
        pm_id = card.stripe_payment_method_id
        card.delete()
        if was_default:
            nxt = (
                SavedCard.objects.select_for_update()
                .filter(contact_id=contact_id)
                .order_by("-last_used_at", "-created_at")
                .first()
            )
            if nxt is not None:
                SavedCard.objects.filter(pk=nxt.pk).update(is_default=True)
    try:
        _stripe().PaymentMethod.detach(pm_id)
    except Exception:
        # The vault and the orders are already safe; Stripe still holding the card can
        # only mean it's never charged again from here. Logged for the office, not raised.
        logger.exception("detaching a removed card from Stripe failed (contact %s)", contact_id)
    return [p.lead for p in holding]
