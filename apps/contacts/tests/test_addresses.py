import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AddressFactory
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


def test_effective_billing_address_follows_toggle():
    primary = AddressFactory(line1="Primary")
    billing = AddressFactory(line1="Billing")
    c = ContactFactory(
        primary_address=primary, billing_address=billing, billing_same_as_primary=True
    )
    assert c.effective_billing_address == primary
    c.billing_same_as_primary = False
    assert c.effective_billing_address == billing


def test_contact_update_sets_billing_same_as_primary(client):
    c = ContactFactory(billing_same_as_primary=True)
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[c.pk]), {"billing_same_as_primary": "false"})
    assert resp.status_code == 200
    c.refresh_from_db()
    assert c.billing_same_as_primary is False
