import datetime

import factory

from apps.contacts.factories import ContactFactory
from apps.reservations.factories import ReservationFactory

from .models import AccountGroup, BillingAccount, Invoice, InvoiceLine, Terms


class BillingAccountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BillingAccount

    contact = factory.SubFactory(ContactFactory)
    name = factory.Sequence(lambda n: f"Billing Account {n}")
    terms = Terms.NET_30


class AccountGroupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AccountGroup

    account = factory.SubFactory(BillingAccountFactory)
    name = factory.Sequence(lambda n: f"Group {n}")
    invoice_email = factory.Sequence(lambda n: f"billing{n}@example.com")


class InvoiceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Invoice

    group = factory.SubFactory(AccountGroupFactory)
    invoice_date = factory.LazyFunction(datetime.date.today)
    due_date = factory.LazyFunction(lambda: datetime.date.today() + datetime.timedelta(days=30))
    total = 0


class InvoiceLineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = InvoiceLine

    invoice = factory.SubFactory(InvoiceFactory)
    reservation = factory.SubFactory(ReservationFactory)
    description = "x"
    amount = "1.00"
