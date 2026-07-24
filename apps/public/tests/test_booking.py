import pytest

from apps.leads.models import Lead
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db

VALID = {
    "name": "Jane Rider",
    "email": "jane@example.com",
    "phone": "2024242600",
    "pickup_date": "2026-09-01",
    "pickup_time": "14:30",
    "passengers": "12",
    "service": "Wedding Transportation",
    "notes": "DCA to venue",
    "company": "",  # honeypot — must stay empty
}


def test_valid_booking_creates_lead_and_notifies(client):
    resp = client.post("/bookings/", VALID)
    assert resp.status_code == 302
    lead = Lead.objects.get()
    assert lead.status == Lead.Status.NEW
    assert lead.channel == "website"
    assert lead.contact.name == "Jane Rider"
    assert lead.reservations.count() == 1
    assert Notification.objects.filter(kind=Notification.Kind.NEW_LEAD).exists()


def test_honeypot_blocks_spam(client):
    resp = client.post("/bookings/", {**VALID, "company": "spammy"})
    assert Lead.objects.count() == 0
    assert resp.status_code in (200, 302)


def test_missing_required_field_shows_errors(client):
    resp = client.post("/bookings/", {**VALID, "name": ""})
    assert resp.status_code == 200
    assert Lead.objects.count() == 0
