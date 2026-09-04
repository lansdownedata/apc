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
