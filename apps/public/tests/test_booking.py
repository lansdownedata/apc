import pytest
from django.core.cache import cache

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


def test_missing_contact_channel_rejected(client):
    resp = client.post("/bookings/", {**VALID, "email": "", "phone": ""})
    assert resp.status_code == 200
    assert resp.context["form"].errors
    assert Lead.objects.count() == 0


def test_bookings_throttled_after_limit(client):
    cache.clear()
    for i in range(5):
        resp = client.post("/bookings/", {**VALID, "email": f"jane{i}@example.com"})
        assert resp.status_code == 302
    assert Lead.objects.count() == 5

    resp = client.post("/bookings/", {**VALID, "email": "jane6@example.com"})
    assert resp.status_code == 200
    assert resp.context["form"].errors
    assert Lead.objects.count() == 5


def test_invalid_submissions_never_trip_throttle(client):
    """Ordinary validation failures (e.g. a mis-fumbled required field) must not
    count toward the per-IP throttle — only a created Lead or a tripped honeypot
    should. A legit visitor retrying a typo shouldn't get locked out.
    """
    cache.clear()
    for _ in range(6):
        resp = client.post("/bookings/", {**VALID, "name": ""})
        assert resp.status_code == 200
        assert Lead.objects.count() == 0
        assert "Too many requests" not in resp.content.decode()
        assert resp.context["form"].errors

    # The throttle counter is still at zero, so a valid submission right after
    # the 6 failures still succeeds.
    resp = client.post("/bookings/", {**VALID, "email": "still-fine@example.com"})
    assert resp.status_code == 302
    assert Lead.objects.count() == 1
