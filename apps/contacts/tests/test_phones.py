"""A contact's phone numbers, and email as the key that finds a returning customer.

`Contact.phone` stays the one number Podium texts (every messaging path reads it);
`ContactPhone` rows hold every number on file, with exactly one marked `texting`.
"""

import pytest

from apps.contacts import services
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact, ContactPhone

pytestmark = pytest.mark.django_db


# --- the texting number mirrors Contact.phone ---------------------------------------


def test_a_new_contacts_phone_becomes_its_texting_number():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    (row,) = contact.phones.all()
    assert row.number == "+16175550207"
    assert row.texting is True
    assert row.label == ContactPhone.Label.MOBILE


def test_a_contact_with_no_phone_has_no_numbers():
    assert Contact.objects.create(name="Ada").phones.count() == 0


def test_changing_contact_phone_keeps_the_old_number_and_moves_texting():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    contact.phone = "+12025550134"
    contact.save(update_fields=["phone", "updated_at"])
    numbers = {p.number: p.texting for p in contact.phones.all()}
    assert numbers == {"+16175550207": False, "+12025550134": True}


def test_saving_other_fields_does_not_touch_the_numbers(django_assert_num_queries):
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    contact = Contact.objects.get(pk=contact.pk)
    contact.name = "Ada Lovelace"
    with django_assert_num_queries(1):
        contact.save(update_fields=["name", "updated_at"])


# --- managing numbers ---------------------------------------------------------------


def test_add_phone_keeps_the_texting_number():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    services.add_phone(contact, "(202) 555-0134", label=ContactPhone.Label.WORK)
    contact.refresh_from_db()
    assert contact.phone == "+16175550207"
    work = contact.phones.get(number="+12025550134")
    assert work.label == ContactPhone.Label.WORK
    assert work.texting is False


def test_add_phone_to_a_contact_with_none_makes_it_the_texting_number():
    contact = Contact.objects.create(name="Ada")
    services.add_phone(contact, "(202) 555-0134", label=ContactPhone.Label.HOME)
    contact.refresh_from_db()
    assert contact.phone == "+12025550134"
    assert contact.phones.get().label == ContactPhone.Label.HOME


def test_add_phone_twice_is_one_row():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    services.add_phone(contact, "617-555-0207")
    assert contact.phones.count() == 1


def test_add_phone_can_take_over_texting():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    services.add_phone(contact, "(202) 555-0134", texting=True)
    contact.refresh_from_db()
    assert contact.phone == "+12025550134"
    assert contact.phones.filter(texting=True).count() == 1


def test_use_for_texting_moves_contact_phone():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    work = services.add_phone(contact, "(202) 555-0134")
    services.use_for_texting(work)
    contact.refresh_from_db()
    assert contact.phone == "+12025550134"
    assert list(contact.phones.filter(texting=True)) == [work]


def test_editing_the_texting_numbers_digits_updates_contact_phone():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    row = contact.phones.get()
    services.update_phone(row, number="(202) 555-0134", label=ContactPhone.Label.WORK)
    contact.refresh_from_db()
    assert contact.phone == "+12025550134"
    assert contact.phones.count() == 1


def test_the_texting_number_cannot_be_removed_while_others_exist():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    services.add_phone(contact, "(202) 555-0134")
    with pytest.raises(services.PhoneError):
        services.remove_phone(contact.phones.get(texting=True))


def test_removing_the_only_number_clears_contact_phone():
    contact = Contact.objects.create(name="Ada", phone="+16175550207")
    services.remove_phone(contact.phones.get())
    contact.refresh_from_db()
    assert contact.phone == ""
    assert contact.phones.count() == 0


def test_add_phone_rejects_an_undiallable_number():
    contact = Contact.objects.create(name="Ada")
    with pytest.raises(services.PhoneError):
        services.add_phone(contact, "12")


# --- email is the key ---------------------------------------------------------------


def test_email_finds_the_contact_even_when_the_phone_differs():
    jane = ContactFactory(email="jane@example.com", phone="+16175550207")
    got = Contact.objects.match_or_create(
        name="Jane Rider", email="Jane@Example.com", phone="(202) 555-0134"
    )
    assert got == jane
    assert Contact.objects.count() == 1


def test_a_new_number_on_a_known_email_is_added_not_swapped():
    jane = ContactFactory(email="jane@example.com", phone="+16175550207")
    Contact.objects.match_or_create(name="Jane", email="jane@example.com", phone="(202) 555-0134")
    jane.refresh_from_db()
    assert jane.phone == "+16175550207"
    assert set(jane.phones.values_list("number", flat=True)) == {"+16175550207", "+12025550134"}


def test_the_most_recent_name_wins():
    jane = ContactFactory(name="Jane Doe", email="jane@example.com")
    Contact.objects.match_or_create(name="Jane Rider", email="jane@example.com")
    jane.refresh_from_db()
    assert jane.name == "Jane Rider"


def test_a_phone_only_match_never_renames():
    """Without an email nobody has confirmed it is the same person (shared numbers)."""
    jane = ContactFactory(name="Jane Doe", email="jane@example.com", phone="+16175550207")
    Contact.objects.match_or_create(name="Sam Doe", phone="(617) 555-0207")
    jane.refresh_from_db()
    assert jane.name == "Jane Doe"


def test_a_phone_match_with_a_different_email_is_a_new_contact():
    ContactFactory(email="jane@example.com", phone="+16175550207")
    got = Contact.objects.match_or_create(
        name="Sam", email="sam@example.com", phone="(617) 555-0207"
    )
    assert got.email == "sam@example.com"
    assert Contact.objects.count() == 2


def test_with_no_email_the_phone_still_matches_any_number_on_file():
    jane = ContactFactory(email="jane@example.com", phone="+16175550207")
    services.add_phone(jane, "(202) 555-0134")
    assert Contact.objects.match_or_create(name="?", phone="202-555-0134") == jane


def test_search_finds_a_contact_by_a_secondary_number():
    jane = ContactFactory(email="jane@example.com", phone="+16175550207")
    services.add_phone(jane, "(202) 555-0134")
    assert list(Contact.objects.search("555-0134")) == [jane]
