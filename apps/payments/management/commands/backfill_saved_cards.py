"""APC-47 — move existing orders' cards into the customer card vault, where Stripe allows.

Before the vault each order (PaymentPlan) made its own Stripe customer. Stripe can't move a
card between customers, so a contact's cards can only join the vault when every one of the
contact's orders is on the same Stripe customer, and no other contact already owns it: that
customer becomes the contact's, and its cards are recorded. Anything else is reported and
left exactly as it is; those cards stay listed read-only on the profile.

Dry run unless `--apply` is passed: it prints what it would do and makes no change and no
Stripe call. Run it, read it, then apply — against a copy first.
"""

from __future__ import annotations

import stripe
from django.core.management.base import BaseCommand

from apps.contacts.models import Contact
from apps.payments import cards
from apps.payments.models import PaymentPlan, SavedCard


class Command(BaseCommand):
    help = "Backfill the customer card vault from existing orders (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Report only (the default without --apply)."
        )

    def handle(self, *args, apply=False, dry_run=False, **options):
        apply = apply and not dry_run
        mode = "APPLY" if apply else "DRY RUN"
        self.stdout.write(f"{mode}: backfilling saved cards")
        plans = (
            PaymentPlan.objects.exclude(stripe_customer_id="")
            .select_related("lead__contact")
            .order_by("-updated_at")
        )
        by_contact: dict[int, list[PaymentPlan]] = {}
        for plan in plans:
            by_contact.setdefault(plan.lead.contact_id, []).append(plan)

        self.adopted = self.recorded = self.left = self.skipped = 0
        for contact in Contact.objects.filter(pk__in=by_contact).order_by("pk"):
            self._contact(contact, by_contact[contact.pk], apply=apply)
        cards_word = str(self.recorded) if apply else f"up to {self.recorded}"
        self.stdout.write(
            f"{mode} done: {self.adopted} contacts adopted, {cards_word} cards, {self.left} left"
            f" as they are, {self.skipped} skipped."
        )

    def _say(self, line: str) -> None:
        self.stdout.write(line)

    def _contact(self, contact, rows, *, apply: bool) -> None:
        customers = {p.stripe_customer_id for p in rows}
        if contact.stripe_customer_id:
            customers.add(contact.stripe_customer_id)
        if len(customers) > 1:
            self.left += 1
            self._say(
                f"  {contact} (#{contact.pk}): orders on {len(customers)} Stripe customers"
                " — left as they are; their cards stay read-only."
            )
            return
        (customer,) = customers
        owner = Contact.objects.filter(stripe_customer_id=customer).exclude(pk=contact.pk).first()
        if owner is not None:
            self.left += 1
            self._say(
                f"  {contact} (#{contact.pk}): {customer} already belongs to {owner}"
                f" (#{owner.pk}) — left as they are."
            )
            return
        if not contact.stripe_customer_id:
            self.adopted += 1
            self._say(
                f"  {contact} (#{contact.pk}): {'adopting' if apply else 'would adopt'} {customer}"
            )
            if apply:
                contact.stripe_customer_id = customer
                contact.save(update_fields=["stripe_customer_id", "updated_at"])
        pm_ids = list(
            dict.fromkeys(p.stripe_payment_method_id for p in rows if p.stripe_payment_method_id)
        )
        for pm_id in pm_ids:
            saved = SavedCard.objects.filter(stripe_payment_method_id=pm_id).first()
            if saved is not None:
                if saved.contact_id != contact.pk:
                    self.skipped += 1
                    self._say(f"    skipped {pm_id}: saved for another contact")
                continue
            if not apply:
                self.recorded += 1
                self._say(f"    would record card {pm_id} (if Stripe still has it)")
                continue
            self._record(contact, customer, pm_id)

    def _record(self, contact, customer: str, pm_id: str) -> None:
        try:
            pm = cards._stripe().PaymentMethod.retrieve(pm_id)
        except stripe.error.StripeError as exc:
            self.skipped += 1
            self._say(f"    skipped {pm_id}: {exc.user_message or 'not found'}")
            return
        if getattr(pm, "customer", None) != customer:
            self.skipped += 1
            self._say(f"    skipped {pm_id}: not attached to {customer}")
            return
        try:
            cards._record(contact, pm, used=False)
        except cards.CardError as exc:
            self.skipped += 1
            self._say(f"    skipped {pm_id}: {exc}")
            return
        self.recorded += 1
        self._say(f"    recorded card ····{pm.card.last4}")
