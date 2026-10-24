"""APC-47 — move existing orders' cards into the customer card vault, where Stripe allows.

Before the vault each order (PaymentPlan) made its own Stripe customer. Stripe can't move a
card between customers, so a contact's cards can only join the vault when every one of the
contact's orders is on the same Stripe customer: that customer becomes the contact's, and
its cards are recorded. A contact whose orders are on different customers is reported and
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

        adopted = recorded = left = skipped = 0
        for contact in Contact.objects.filter(pk__in=by_contact).order_by("pk"):
            rows = by_contact[contact.pk]
            customers = {p.stripe_customer_id for p in rows}
            if contact.stripe_customer_id:
                customers.add(contact.stripe_customer_id)
            if len(customers) > 1:
                left += 1
                self.stdout.write(
                    f"  {contact} (#{contact.pk}): orders on {len(customers)} Stripe customers"
                    " — left as they are; their cards stay read-only."
                )
                continue
            (customer,) = customers
            pm_ids = list(
                dict.fromkeys(
                    p.stripe_payment_method_id for p in rows if p.stripe_payment_method_id
                )
            )
            new_pms = [
                pm
                for pm in pm_ids
                if not SavedCard.objects.filter(stripe_payment_method_id=pm).exists()
            ]
            if not contact.stripe_customer_id:
                adopted += 1
                verb = "adopting" if apply else "would adopt"
                self.stdout.write(f"  {contact} (#{contact.pk}): {verb} {customer}")
                if apply:
                    contact.stripe_customer_id = customer
                    contact.save(update_fields=["stripe_customer_id", "updated_at"])
            for pm_id in new_pms:
                if not apply:
                    self.stdout.write(f"    would record card {pm_id}")
                    recorded += 1
                    continue
                try:
                    pm = cards._stripe().PaymentMethod.retrieve(pm_id)
                except stripe.error.StripeError as exc:
                    skipped += 1
                    self.stdout.write(f"    skipped {pm_id}: {exc.user_message or 'not found'}")
                    continue
                if getattr(pm, "customer", None) != customer:
                    skipped += 1
                    self.stdout.write(f"    skipped {pm_id}: not attached to {customer}")
                    continue
                cards._record(contact, pm, used=False)
                recorded += 1
                self.stdout.write(f"    recorded card ····{pm.card.last4}")
        self.stdout.write(
            f"{mode} done: {adopted} contacts adopted, {recorded} cards, {left} left as they are,"
            f" {skipped} skipped."
        )
