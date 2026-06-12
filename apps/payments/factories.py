from decimal import Decimal

import factory

from apps.leads.factories import LeadFactory

from .models import Charge, PaymentPlan


class PaymentPlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PaymentPlan

    lead = factory.SubFactory(LeadFactory)
    deposit_pct = 50
    quote_total = Decimal("0.00")


class ChargeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Charge

    plan = factory.SubFactory(PaymentPlanFactory)
    kind = Charge.Kind.DEPOSIT
    amount = Decimal("100.00")
    idempotency_key = factory.Sequence(lambda n: f"key-{n}")
