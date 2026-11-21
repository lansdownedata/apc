"""APC-20 — the board row and drawer show whether the affiliate has acknowledged the
T-48h trip confirmation (Assignment.affiliate_confirmed_at)."""

from datetime import time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
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


def test_board_row_shows_a_check_once_the_affiliate_confirms(logged_in_client):
    trip = _trip()
    AssignmentFactory(
        reservation=trip, status=Assignment.Status.CONFIRMED, affiliate_confirmed_at=timezone.now()
    )

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert b"ti-circle-check-filled" in resp.content


def test_board_row_shows_nothing_before_the_affiliate_confirms(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})

    assert b"ti-circle-check-filled" not in resp.content


# --- drawer panel --------------------------------------------------------------------


def test_panel_shows_affiliate_confirmed_state(logged_in_client):
    trip = _trip()
    AssignmentFactory(
        reservation=trip, status=Assignment.Status.CONFIRMED, affiliate_confirmed_at=timezone.now()
    )

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Confirmed" in resp.content
    assert b"Not yet confirmed by the affiliate" not in resp.content


def test_panel_shows_affiliate_unconfirmed_state(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Not yet confirmed by the affiliate" in resp.content


def test_panel_omits_the_ack_state_for_in_house(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, in_house=True)

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Not yet confirmed by the affiliate" not in resp.content
