import factory

from apps.core.choices import Channel

from .models import Contact, ContactPhone


class ContactFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Contact
        skip_postgeneration_save = True

    name = factory.Faker("name")
    email = factory.Faker("email")
    # 202-555-01xx is a reserved, always-valid US test range.
    phone = factory.Sequence(lambda n: f"+1202555{n % 100:02d}00"[:12])
    channel = Channel.WEBSITE


class ContactPhoneFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ContactPhone

    contact = factory.SubFactory(ContactFactory)
    e164 = factory.Sequence(lambda n: f"+1305555{n % 100:02d}00"[:12])
    label = "mobile"
    is_primary = False
