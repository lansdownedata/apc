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
