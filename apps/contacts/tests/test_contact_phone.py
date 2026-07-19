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


def test_add_phone_owned_by_another_contact_returns_none_and_does_not_steal():
    owner = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")

    result = other.add_phone("(202) 555-0100")

    assert result is None
    assert owner.phones.count() == 1
    owned = owner.phones.get()
    assert owned.contact_id == owner.pk
    assert owned.e164 == "+12025550100"
    assert other.phones.count() == 1
    assert not other.phones.filter(e164="+12025550100").exists()


def test_set_primary_phone_owned_by_another_contact_returns_none_and_does_not_steal():
    owner = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")

    result = other.set_primary_phone("(202) 555-0100")

    assert result is None
    owned = owner.phones.get(e164="+12025550100")
    assert owned.contact_id == owner.pk
    assert owned.is_primary is True
    assert not other.phones.filter(e164="+12025550100").exists()


def test_set_primary_phone_on_number_self_already_owns_still_works():
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("(305) 555-0199", label="work")

    result = contact.set_primary_phone("(305) 555-0199")

    assert result is not None
    assert result.contact_id == contact.pk
    assert contact.phone == "+13055550199"
    assert contact.phones.filter(is_primary=True).count() == 1


def test_add_phone_refusal_is_logged(caplog):
    """The webhook path (`_enrich` → `add_phone`) drops refused numbers silently
    otherwise — nothing to grep for when a Podium contact's number turns out to
    already belong to someone else."""
    owner = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")

    with caplog.at_level("INFO", logger="apps.contacts.models"):
        other.add_phone("(202) 555-0100")

    assert any(
        str(owner.pk) in r.message and str(other.pk) in r.message and "+12025550100" in r.message
        for r in caplog.records
    )


def test_set_primary_phone_refusal_is_logged(caplog):
    owner = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")

    with caplog.at_level("INFO", logger="apps.contacts.models"):
        other.set_primary_phone("(202) 555-0100")

    assert any(
        str(owner.pk) in r.message and str(other.pk) in r.message and "+12025550100" in r.message
        for r in caplog.records
    )


def test_malformed_phone_does_not_log(caplog):
    contact = ContactFactory(phone="(202) 555-0100")

    with caplog.at_level("INFO", logger="apps.contacts.models"):
        assert contact.add_phone("junk") is None
        assert contact.set_primary_phone("junk") is None

    assert caplog.records == []


def test_add_phone_for_number_self_already_owns_is_idempotent():
    contact = ContactFactory(phone="(202) 555-0100")

    result = contact.add_phone("(202) 555-0100")

    assert result is not None
    assert result.contact_id == contact.pk
    assert contact.phones.count() == 1


# --- ContactPhone.objects.matching() — shared search entry point ----------------------


def test_matching_finds_by_digit_substring():
    owner = ContactFactory(phone="(202) 555-0187")
    matches = ContactPhone.objects.matching("555-0187")
    assert list(matches.values_list("contact_id", flat=True)) == [owner.pk]


def test_matching_returns_empty_for_non_phone_text():
    ContactFactory(phone="(202) 555-0005")
    # "Suite 5" collapses to digit "5" if naively stripped — must not match via that.
    assert ContactPhone.objects.matching("Suite 5").count() == 0


def test_matching_returns_empty_for_blank_term():
    ContactFactory(phone="(202) 555-0100")
    assert ContactPhone.objects.matching("").count() == 0


def test_matching_returns_empty_for_short_digit_run():
    ContactFactory(phone="(202) 555-0100")
    assert ContactPhone.objects.matching("10").count() == 0
