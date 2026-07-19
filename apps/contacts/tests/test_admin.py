"""Admin search must not 500 now that `Contact.phone` is a property, not a field."""

import pytest

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client_(client):
    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    return client


def test_changelist_search_by_phone_digits_does_not_500(admin_client_):
    ContactFactory(phone="(202) 555-0100")
    resp = admin_client_.get("/admin/contacts/contact/", {"q": "555"})
    assert resp.status_code == 200


def test_changelist_search_by_name_still_works(admin_client_):
    ContactFactory(name="Sarah Reyes")
    resp = admin_client_.get("/admin/contacts/contact/", {"q": "Sarah"})
    assert resp.status_code == 200
    assert "Sarah Reyes" in resp.content.decode()
