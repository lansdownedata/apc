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


def test_address_update_lazily_creates_and_writes(client):
    c = ContactFactory()
    assert c.primary_address_id is None
    client.force_login(UserFactory())
    resp = client.post(
        reverse("contact_address_update", args=[c.pk, "primary"]),
        {
            "line1": "14 Beacon St",
            "city": "Boston",
            "state": "MA",
            "postal": "02108",
            "latitude": "42.3583",
            "longitude": "-71.0603",
            "place_id": "456",
        },
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    c.refresh_from_db()
    assert c.primary_address is not None
    assert c.primary_address.line1 == "14 Beacon St"
    assert c.primary_address.city == "Boston"
    assert str(c.primary_address.latitude) == "42.358300"
    assert c.primary_address.locationiq_place_id == "456"


def test_address_update_reuses_existing_slot_row(client):
    c = ContactFactory()
    client.force_login(UserFactory())
    url = reverse("contact_address_update", args=[c.pk, "primary"])
    client.post(url, {"line1": "First"})
    c.refresh_from_db()
    first_id = c.primary_address_id
    client.post(url, {"line1": "Second"})
    c.refresh_from_db()
    assert c.primary_address_id == first_id  # same row, updated
    assert c.primary_address.line1 == "Second"


def test_address_update_rejects_bad_slot(client):
    c = ContactFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_address_update", args=[c.pk, "nonsense"]), {})
    assert resp.status_code == 404
