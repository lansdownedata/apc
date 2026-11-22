import factory
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from apps.contacts.factories import ContactFactory

from .models import CustomerAccount


class CustomerAccountFactory(factory.django.DjangoModelFactory):
    """An active account whose password is `"correct horse battery"`."""

    class Meta:
        model = CustomerAccount

    contact = factory.SubFactory(ContactFactory)
    password = factory.LazyFunction(lambda: make_password("correct horse battery"))
    registered_at = factory.LazyFunction(timezone.now)

    class Params:
        invited = factory.Trait(
            invited_at=factory.LazyFunction(timezone.now),
            registered_at=None,
            password=factory.LazyFunction(lambda: make_password(None)),
        )
        revoked = factory.Trait(revoked_at=factory.LazyFunction(timezone.now))
