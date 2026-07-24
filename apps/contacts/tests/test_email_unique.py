import pytest
from django.db.utils import IntegrityError

from apps.contacts.models import Contact

pytestmark = pytest.mark.django_db


def test_blank_email_stored_as_null_and_allows_many():
    a = Contact.objects.create(name="A", email="")
    b = Contact.objects.create(name="B", email="")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.email is None and b.email is None  # many NULLs allowed


def test_email_lowercased_on_save():
    c = Contact.objects.create(name="C", email="Priya@Example.COM")
    c.refresh_from_db()
    assert c.email == "priya@example.com"


def test_duplicate_email_rejected_case_insensitive():
    Contact.objects.create(name="A", email="dup@example.com")
    with pytest.raises(IntegrityError):
        Contact.objects.create(name="B", email="DUP@example.com")
