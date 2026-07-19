import factory

from apps.core.choices import Channel

from .models import Contact, ContactPhone

# `phonenumbers` accepts any 4-digit line number under the 555 exchange (not just the
# fictional 0100-0199 block), so rotating the line number across ten valid NANP area
# codes yields 100,000 unique, always-valid US test numbers — e.g. +12025550000,
# +13055550001, ... — without ever wrapping within a test session. The previous
# `n % 100` sequence wrapped after 100 calls and, thanks to refuse-to-steal, silently
# produced a phone-less contact on the 101st call instead of raising.
_TEST_AREA_CODES = (202, 305, 212, 310, 415, 617, 646, 713, 818, 954)


def _unique_test_phone(n: int) -> str:
    area = _TEST_AREA_CODES[n % len(_TEST_AREA_CODES)]
    line = n // len(_TEST_AREA_CODES)
    return f"+1{area}555{line:04d}"


class ContactFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Contact
        skip_postgeneration_save = True

    name = factory.Faker("name")
    email = factory.Faker("email")
    phone = factory.Sequence(_unique_test_phone)
    channel = Channel.WEBSITE


class ContactPhoneFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ContactPhone

    contact = factory.SubFactory(ContactFactory)
    e164 = factory.Sequence(lambda n: f"+1305555{n % 100:02d}00"[:12])
    label = "mobile"
    is_primary = False
