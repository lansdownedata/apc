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


def test_find_match_by_phone():
    c = ContactFactory(phone="(202) 555-0100", email="a@example.com")
    assert Contact.objects.find_match(phone="(202) 555-0100", email="") == c


def test_find_match_by_email_case_insensitive():
    c = ContactFactory(phone="", email="Bride@Example.com")
    assert Contact.objects.find_match(phone="", email="bride@example.com") == c


def test_find_match_none_when_blank_or_no_hit():
    ContactFactory(phone="(202) 555-0100", email="a@example.com")
    assert Contact.objects.find_match(phone="", email="") is None
    assert Contact.objects.find_match(phone="(202) 555-9999", email="") is None


def test_match_or_create_reuses_existing():
    c = ContactFactory(phone="(202) 555-0100", email="a@example.com")
    got = Contact.objects.match_or_create(
        name="Someone Else", phone="(202) 555-0100", email="new@example.com"
    )
    assert got == c
    assert Contact.objects.count() == 1


def test_match_or_create_creates_when_no_match():
    got = Contact.objects.match_or_create(
        name="Sarah Boyne", phone="(703) 555-0148", email="sarah@example.com",
        channel=Channel.PHONE,
    )
    assert got.pk is not None
    assert got.channel == Channel.PHONE
    assert Contact.objects.count() == 1
