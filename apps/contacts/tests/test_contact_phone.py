import pytest
from django.db import IntegrityError

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact, ContactPhone

pytestmark = pytest.mark.django_db


def test_phone_property_returns_primary_e164():
    contact = ContactFactory(phone="(202) 555-0100")
    assert contact.phone == "+12025550100"


def test_phone_property_is_blank_when_no_numbers():
    contact = Contact.objects.create(name="No Phone")
    assert contact.phone == ""


def test_create_with_phone_normalizes_and_marks_primary():
    contact = Contact.objects.create(name="Sarah Reyes", phone="202-555-0100")
    phone = contact.phones.get()
    assert phone.e164 == "+12025550100"
    assert phone.is_primary is True


def test_create_with_unparseable_phone_creates_no_row():
    contact = Contact.objects.create(name="Bad Number", phone="not a phone")
    assert contact.phones.count() == 0
    assert contact.phone == ""


def test_setter_replaces_existing_primary():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.phone = "(305) 555-0199"
    contact.save()
    assert contact.phone == "+13055550199"
    assert contact.phones.filter(is_primary=True).count() == 1


def test_add_phone_secondary_does_not_steal_primary():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("(305) 555-0199", label="work")
    assert contact.phone == "+12025550100"
    assert contact.phones.count() == 2
    assert contact.phones.get(e164="+13055550199").is_primary is False


def test_add_phone_as_primary_demotes_the_old_one():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("(305) 555-0199", primary=True)
    assert contact.phone == "+13055550199"
    assert contact.phones.filter(is_primary=True).count() == 1


def test_add_phone_is_idempotent_for_same_number():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("+1 202 555 0100")
    assert contact.phones.count() == 1


def test_e164_is_globally_unique():
    ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")
    with pytest.raises(IntegrityError):
        ContactPhone.objects.create(contact=other, e164="+12025550100")


def test_phones_ordered_primary_first():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("(305) 555-0199", label="work")
    assert [p.is_primary for p in contact.phones.all()] == [True, False]
