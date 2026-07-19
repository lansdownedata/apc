import pytest

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.core.choices import Channel

pytestmark = pytest.mark.django_db


def test_str_is_name_without_company():
    contact = ContactFactory(name="Sarah Reyes", company="")
    assert str(contact) == "Sarah Reyes"


def test_str_includes_company_when_present():
    contact = ContactFactory(name="Denise Walker", company="Beltway Capital")
    assert str(contact) == "Denise Walker · Beltway Capital"


def test_defaults():
    contact = Contact.objects.create(name="James Tran", channel=Channel.WEBSITE)
    assert contact.company == ""
    assert contact.la_account_id == ""
    assert contact.created_at is not None


def test_find_match_by_phone_across_formats():
    """The headline regression: agent-typed and Podium-supplied formats must match."""
    c = ContactFactory(phone="(202) 555-0100")
    assert Contact.objects.find_match(phone="+12025550100") == c
    assert Contact.objects.find_match(phone="202-555-0100") == c
    assert Contact.objects.find_match(phone="2025550100") == c


def test_find_match_by_secondary_number():
    c = ContactFactory(phone="(202) 555-0100")
    c.add_phone("(305) 555-0199", label="work")
    assert Contact.objects.find_match(phone="+13055550199") == c


def test_find_match_by_email_case_insensitive():
    c = ContactFactory(phone="", email="Bride@Example.com")
    assert Contact.objects.find_match(email="bride@example.com") == c


def test_find_match_by_podium_uid_wins_over_phone():
    by_uid = ContactFactory(phone="(202) 555-0100", podium_contact_uid="pod-1")
    ContactFactory(phone="(305) 555-0199")
    assert Contact.objects.find_match(phone="+13055550199", podium_uid="pod-1") == by_uid


def test_find_match_none_when_blank_or_no_hit():
    ContactFactory(phone="(202) 555-0100")
    assert Contact.objects.find_match() is None
    assert Contact.objects.find_match(phone="+12025559999") is None


def test_find_match_ignores_unparseable_phone():
    ContactFactory(phone="(202) 555-0100")
    assert Contact.objects.find_match(phone="not a phone") is None


def test_match_or_create_returns_existing_on_phone_format_difference():
    existing = ContactFactory(phone="(202) 555-0100")
    got = Contact.objects.match_or_create(name="Someone Else", phone="+12025550100")
    assert got == existing
    assert Contact.objects.count() == 1


def test_match_or_create_creates_with_normalized_phone():
    contact = Contact.objects.match_or_create(name="New Person", phone="(305) 555-0199")
    assert contact.phone == "+13055550199"


def test_match_or_create_adopts_podium_uid_on_existing_contact():
    existing = ContactFactory(phone="(202) 555-0100", podium_contact_uid="")
    got = Contact.objects.match_or_create(name="Sarah", phone="+12025550100", podium_uid="pod-9")
    got.refresh_from_db()
    assert got == existing
    assert got.podium_contact_uid == "pod-9"
