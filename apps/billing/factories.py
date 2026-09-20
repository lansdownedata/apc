import factory

from apps.contacts.factories import ContactFactory

from .models import AccountGroup, BillingAccount, Terms


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
