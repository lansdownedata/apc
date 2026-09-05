"""Public token-keyed acknowledgement pages (APC-18 / APC-19 / APC-20)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _future(days=20):
    return (timezone.now() + timedelta(days=days)).date()


# --- customer trip confirmation (APC-19) --------------------------------------------
# The link covers a customer's whole day, so these build through `_day_url`.


def _day_url(res):
    return reverse(
        "trip_confirm", args=[ack.make_trip_day_ack_token(res.lead.contact, res.pickup_date)]
    )


def test_trip_confirm_get_then_post_stamps_confirmed_at(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    url = _day_url(res)

    assert client.get(url).status_code == 200
    resp = client.post(url, {"ack": "on"})
    assert resp.status_code == 302

    res.refresh_from_db()
    assert res.customer_confirmed_at is not None


def test_trip_confirm_is_idempotent(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    url = _day_url(res)
    client.post(url, {"ack": "on"})
    first = ReservationFactory._meta.model.objects.get(pk=res.pk).customer_confirmed_at
    client.post(url, {"ack": "on"})
    res.refresh_from_db()
    assert res.customer_confirmed_at == first


def test_trip_confirm_bad_token_404s(client):
    assert client.get(reverse("trip_confirm", args=["not-a-real-token"])).status_code == 404


def test_trip_confirm_confirms_every_trip_that_day_from_one_checkbox(client):
    contact = ContactFactory()
    day = _future()
    first = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED), pickup_date=day
    )
    second = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED), pickup_date=day
    )

    client.post(_day_url(first), {"ack": "on"})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.customer_confirmed_at is not None
    assert second.customer_confirmed_at is not None


def test_trip_confirm_page_lists_every_trip_and_names_the_contact(client):
    contact = ContactFactory(name="Dana Whitfield")
    day = _future()
    morning = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED),
        pickup_date=day,
        stops=["Dulles International", "The Willard"],
    )
    ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED),
        pickup_date=day,
        stops=["The Willard", "Reagan National"],
    )

    body = client.get(_day_url(morning)).content.decode()

    assert "Dana Whitfield" in body
    assert "Dulles International" in body
    assert "Reagan National" in body


def test_trip_confirm_requires_the_acknowledgement_box(client):
    """`required` is client-side only — an unchecked POST must not confirm."""
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())

    resp = client.post(_day_url(res), {})

    assert resp.status_code == 200
    res.refresh_from_db()
    assert res.customer_confirmed_at is None


def test_trip_confirm_never_shows_driver_or_affiliate_detail(client):
    """Trip details only — no farm-out company, no driver, no payout (client rule)."""
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment

    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED, payout=250)

    body = client.get(_day_url(res)).content.decode()

    assert a.vendor.name not in body
    assert "Payout" not in body


def test_trip_confirm_shows_nothing_to_confirm_when_the_day_is_cancelled(client):
    res = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future(), trip_status="cancelled"
    )
    url = _day_url(res)
    resp = client.get(url)
    assert resp.status_code == 200
    assert b"cancelled" in resp.content.lower()
    client.post(url, {"ack": "on"})
    res.refresh_from_db()
    assert res.customer_confirmed_at is None


# --- affiliate trip confirmation (APC-20) -------------------------------------------


def test_affiliate_confirm_stamps_the_assignment(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED)
    url = reverse("affiliate_trip_confirm", args=[ack.make_affiliate_ack_token(a)])

    assert client.get(url).status_code == 200
    client.post(url, {"ack": "on"})
    a.refresh_from_db()
    assert a.affiliate_confirmed_at is not None


def test_affiliate_confirm_inactive_assignment_does_not_stamp(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.WITHDRAWN)
    url = reverse("affiliate_trip_confirm", args=[ack.make_affiliate_ack_token(a)])
    client.post(url, {"ack": "on"})
    a.refresh_from_db()
    assert a.affiliate_confirmed_at is None


# --- wedding day-of details (APC-18) ----------------------------------------------


def test_wedding_details_writes_the_lead_fields(client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    res = ReservationFactory(lead=lead, pickup_date=_future(), source_leg_id="guests-in")
    url = reverse("wedding_details", args=[ack.make_wedding_details_token(res)])

    assert client.get(url).status_code == 200
    resp = client.post(
        url,
        {
            "wedding_name": "Boyne–Ellis Wedding",
            "contact_name": "Jamie Planner",
            "contact_phone": "(703) 555-0148",
        },
    )
    assert resp.status_code == 302
    lead.refresh_from_db()
    assert lead.wedding_name == "Boyne–Ellis Wedding"
    assert lead.day_of_contact_name == "Jamie Planner"
    assert lead.day_of_contact_phone  # normalised to E.164 or kept as typed
