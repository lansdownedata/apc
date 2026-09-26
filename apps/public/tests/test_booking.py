import pytest
from django.core.cache import cache

from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.public.views import BOOKING_THROTTLE_LIMIT

pytestmark = pytest.mark.django_db

VALID = {
    "name": "Jane Rider",
    "email": "jane@example.com",
    "phone": "2024242600",
    "pickup_date": "2026-09-01",
    "pickup_time": "14:30",
    "passengers": "12",
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


def test_the_chosen_occasion_lands_on_the_reservation(client):
    """The widget posts a ServiceType id; it must reach the trip, not just validate."""
    from apps.leads.factories import ServiceTypeFactory

    # The per-IP booking throttle lives in the process-wide cache, so every booking POST
    # in the suite counts toward the same window — clear it or this test starves a later one.
    cache.clear()
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    client.post("/bookings/", {**VALID, "service_type": wedding.pk})
    assert Lead.objects.get().reservations.get().service_type == wedding


def test_two_visitors_behind_the_proxy_get_their_own_throttle_budgets(client, settings):
    """The bug this guards: on Heroku REMOTE_ADDR is the router, so every visitor shared
    one bucket and real booking requests were rejected after a handful site-wide."""
    settings.TRUSTED_PROXY_COUNT = 1
    cache.clear()
    for i in range(BOOKING_THROTTLE_LIMIT):
        resp = client.post(
            "/bookings/",
            {**VALID, "email": f"first{i}@example.com"},
            HTTP_X_FORWARDED_FOR="203.0.113.7",
        )
        assert resp.status_code == 302

    # The first visitor is spent...
    spent = client.post(
        "/bookings/",
        {**VALID, "email": "first-blocked@example.com"},
        HTTP_X_FORWARDED_FOR="203.0.113.7",
    )
    assert spent.status_code == 200

    # ...but a different customer on the same site is not.
    other = client.post(
        "/bookings/",
        {**VALID, "email": "second@example.com"},
        HTTP_X_FORWARDED_FOR="198.51.100.4",
    )
    assert other.status_code == 302


def test_a_spoofed_forwarded_header_cannot_buy_a_fresh_budget(client, settings):
    """Heroku appends the real peer, so a client-supplied prefix is ignored."""
    settings.TRUSTED_PROXY_COUNT = 1
    cache.clear()
    for i in range(BOOKING_THROTTLE_LIMIT):
        client.post(
            "/bookings/",
            {**VALID, "email": f"spoof{i}@example.com"},
            HTTP_X_FORWARDED_FOR="203.0.113.7",
        )
    resp = client.post(
        "/bookings/",
        {**VALID, "email": "spoof-again@example.com"},
        HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.7",
    )
    assert resp.status_code == 200


# --- a submission that matches an existing customer: email is the key -------------


def test_a_returning_email_takes_the_name_they_gave_this_time(client):
    """Email is the key; the most recent name on it wins."""
    from apps.contacts.factories import ContactFactory

    jane = ContactFactory(name="Jane Doe", email="jane@example.com")
    client.post("/bookings/", {**VALID, "name": "Jane Rider"})
    jane.refresh_from_db()
    assert Lead.objects.get().contact == jane
    assert jane.name == "Jane Rider"


def test_a_booking_without_an_email_is_refused(client):
    """Email is how we find a returning customer, so the form insists on one."""
    response = client.post("/bookings/", {**VALID, "email": ""})
    assert not Lead.objects.exists()
    assert response.status_code in (200, 302)
