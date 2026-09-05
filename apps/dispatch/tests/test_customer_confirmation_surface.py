"""APC-19 — the board row and drawer show whether the customer has acknowledged the
T-72h trip confirmation (Reservation.customer_confirmed_at)."""

from datetime import time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

DAY = timezone.localdate() + timedelta(days=30)


def _trip(**kw):
    lead = kw.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kw.setdefault("pickup_date", DAY)
    kw.setdefault("pickup_time", time(9, 0))
    return ReservationFactory(lead=lead, **kw)


# --- board row ---------------------------------------------------------------------


def test_board_row_shows_a_check_once_confirmed(logged_in_client):
    _trip(customer_confirmed_at=timezone.now())

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert b"ti-circle-check-filled" in resp.content


def test_board_row_shows_nothing_before_confirmation(logged_in_client):
    _trip()

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert b"ti-circle-check-filled" not in resp.content


# --- drawer panel --------------------------------------------------------------------


def test_panel_shows_confirmed_state(logged_in_client):
    trip = _trip(customer_confirmed_at=timezone.now())

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Confirmed" in resp.content
    assert b"Not yet confirmed" not in resp.content


def test_panel_shows_unconfirmed_state(logged_in_client):
    trip = _trip()

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Not yet confirmed" in resp.content


# --- manual confirmation (APC-19) ----------------------------------------------------
# At T-24h an unconfirmed day leaves the automated cadence and goes on the daily office
# report, where it gets confirmed by phone — this is where that lands.


def test_manual_confirm_stamps_the_trip(logged_in_client):
    trip = _trip()

    resp = logged_in_client.post(reverse("dispatch_confirm_customer", args=[trip.pk]))

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.customer_confirmed_at is not None


def test_manual_confirm_keeps_the_original_timestamp(logged_in_client):
    stamped = timezone.now() - timedelta(days=1)
    trip = _trip(customer_confirmed_at=stamped)

    logged_in_client.post(reverse("dispatch_confirm_customer", args=[trip.pk]))

    trip.refresh_from_db()
    assert trip.customer_confirmed_at == stamped


def test_manual_confirm_requires_login(client):
    trip = _trip()

    resp = client.post(reverse("dispatch_confirm_customer", args=[trip.pk]))

    assert resp.status_code in (302, 403)
    trip.refresh_from_db()
    assert trip.customer_confirmed_at is None


def test_manual_confirm_rejects_get(logged_in_client):
    trip = _trip()

    url = reverse("dispatch_confirm_customer", args=[trip.pk])

    assert logged_in_client.get(url).status_code == 405


def test_panel_offers_the_manual_confirm_button_when_unconfirmed(logged_in_client):
    trip = _trip()

    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    assert "confirm-customer" in body


def test_panel_hides_the_manual_confirm_button_once_confirmed(logged_in_client):
    trip = _trip(customer_confirmed_at=timezone.now())

    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    assert "confirm-customer" not in body
