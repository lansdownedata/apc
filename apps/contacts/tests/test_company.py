import pytest
from django.db.utils import IntegrityError

from apps.contacts.models import Company, Contact

pytestmark = pytest.mark.django_db


def test_get_or_create_by_name_creates_then_reuses():
    a = Company.objects.get_or_create_by_name("Anand Family Office")
    b = Company.objects.get_or_create_by_name("anand family office")  # case-insensitive
    assert a.pk == b.pk
    assert Company.objects.count() == 1


def test_get_or_create_by_name_blank_returns_none():
    assert Company.objects.get_or_create_by_name("") is None
    assert Company.objects.get_or_create_by_name("   ") is None
    assert Company.objects.count() == 0


def test_company_name_case_insensitive_unique():
    Company.objects.create(name="Acme")
    with pytest.raises(IntegrityError):
        Company.objects.create(name="ACME")


def test_contact_company_is_a_company_fk():
    c = Contact.objects.create(name="Priya")
    c.company = Company.objects.get_or_create_by_name("Anand Family Office")
    c.save()
    c.refresh_from_db()
    assert c.company.name == "Anand Family Office"


def test_match_or_create_resolves_company_name_to_fk():
    contact = Contact.objects.match_or_create(name="Priya", company_name="Anand Family Office")
    assert contact.company is not None
    assert contact.company.name == "Anand Family Office"
    # a second contact with the same company reuses the Company row
    other = Contact.objects.match_or_create(name="Raj", company_name="anand family office")
    assert other.company_id == contact.company_id
    assert Company.objects.count() == 1
