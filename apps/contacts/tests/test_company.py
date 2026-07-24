import pytest
from django.db.utils import IntegrityError

from apps.contacts.models import Company

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
