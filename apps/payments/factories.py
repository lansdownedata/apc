from decimal import Decimal

import factory

from apps.leads.factories import LeadFactory

from .models import Charge, PaymentPlan


class PaymentPlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PaymentPlan
        skip_postgeneration_save = True

    lead = factory.SubFactory(LeadFactory)
    deposit_pct = 50
    quote_total = Decimal("0.00")

    @factory.post_generation
    def trips(self, create, extracted, **kwargs):
        """What is owed reads the live trip totals (spec 2026-10-25 §3), so a plan made with
        a bare `quote_total` and no trips would owe nothing. Give it one flat trip worth
        the snapshot unless the lead already has trips."""
        total = Decimal(str(self.quote_total or 0))
        if extracted is False or not create or total <= 0 or self.lead.reservations.exists():
            return  # `trips=False` opts out for a lead that must stay trip-less
        from apps.reservations.factories import ReservationFactory

        ReservationFactory(lead=self.lead, rate=total, hours=1, min_hours=0)


class ChargeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Charge

    plan = factory.SubFactory(PaymentPlanFactory)
    kind = Charge.Kind.DEPOSIT
    amount = Decimal("100.00")
    idempotency_key = factory.Sequence(lambda n: f"key-{n}")
