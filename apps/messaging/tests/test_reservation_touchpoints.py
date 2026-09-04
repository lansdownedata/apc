"""Reservation-anchored touch-points (APC-18-22 engine foundation).

Structure / scheduling / skip-logic only — the message wording is draft copy pending the
client (APC-27), so nothing here asserts a body string.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.models import NotificationConfig, TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

K = TouchPoint.Kind


def _booked_lead(**kw):
    return LeadFactory(status=Lead.Status.BOOKED, **kw)


def _future(days=30):
    return (timezone.now() + timedelta(days=days)).date()


# --- schedule_service_touchpoints -----------------------------------------------------


def test_schedules_customer_confirmation_at_pickup_minus_72h():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(), pickup_time=None)

    touchpoints.schedule_service_touchpoints(lead)

    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER)
    assert tp.lead_id == lead.pk
    assert tp.scheduled_for == res.pickup_at - timedelta(hours=72)


def test_wedding_trip_also_schedules_the_t7d_message():
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, service_type=wedding, pickup_date=_future())

    touchpoints.schedule_service_touchpoints(lead)

    tp = TouchPoint.objects.get(reservation=res, kind=K.WED_FINAL_DETAILS)
    assert tp.scheduled_for == res.pickup_at - timedelta(days=7)


def test_non_wedding_trip_gets_no_wedding_message():
    lead = _booked_lead()
    ReservationFactory(lead=lead, pickup_date=_future())
    touchpoints.schedule_service_touchpoints(lead)
    assert not TouchPoint.objects.filter(kind=K.WED_FINAL_DETAILS).exists()


def test_source_leg_id_marks_a_trip_as_a_wedding():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(), source_leg_id="guests-in")
    touchpoints.schedule_service_touchpoints(lead)
    assert TouchPoint.objects.filter(reservation=res, kind=K.WED_FINAL_DETAILS).exists()


def test_trip_without_a_pickup_date_is_skipped():
    lead = _booked_lead()
    ReservationFactory(lead=lead, pickup_date=None)
    touchpoints.schedule_service_touchpoints(lead)
    assert not TouchPoint.objects.filter(kind=K.TRIP_CONFIRM_CUSTOMER).exists()


def test_scheduling_is_idempotent():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    touchpoints.schedule_service_touchpoints(lead)
    touchpoints.schedule_service_touchpoints(lead)
    assert TouchPoint.objects.filter(reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER).count() == 1


def test_a_past_anchor_is_still_scheduled():
    """A late booking (< 72h out) still deserves a confirmation."""
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=(timezone.now() + timedelta(hours=12)).date())
    touchpoints.schedule_service_touchpoints(lead)
    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER)
    assert tp.scheduled_for < timezone.now()


# --- wiring ---------------------------------------------------------------------------


def test_book_lead_schedules_the_service_touchpoints():
    lead = LeadFactory(status=Lead.Status.NEW)
    ReservationFactory(lead=lead, pickup_date=_future())
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        from apps.leads.services import book_lead

        book_lead(lead)
    assert TouchPoint.objects.filter(lead=lead, kind=K.TRIP_CONFIRM_CUSTOMER).count() == 1


def test_reservation_editor_reschedules_on_a_pickup_date_change():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(10))
    touchpoints.schedule_service_touchpoints(lead)
    original = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER)

    from apps.reservations.drafts import save_reservation_from_draft

    payload = {
        "trip_type": "transfer",
        "date": _future(20).isoformat(),
        "time": "09:00",
        "passengers": 2,
        "vehicle_id": res.vehicle_id,
        "rate": "185",
        "hours": "1",
        "min_hours": "0",
        "stops": [{"address": "A"}, {"address": "B"}],
    }
    save_reservation_from_draft(lead, payload, instance=res)

    original.refresh_from_db()
    assert original.status == TouchPoint.Status.CANCELLED
    fresh = TouchPoint.objects.get(
        reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER, status=TouchPoint.Status.SCHEDULED
    )
    assert fresh.pk != original.pk


def test_reschedule_cancels_an_already_sent_confirmation_and_makes_a_fresh_one():
    """A trip inside its T-72h window already got the confirmation SENT; the pickup then
    moves further out. The old (now-wrong) SENT row must not block a new one — `_ensure`
    treats any non-CANCELLED row as "already handled"."""
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(2))
    sent = TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        status=TouchPoint.Status.SENT,
        scheduled_for=timezone.now() - timedelta(days=1),
        sent_at=timezone.now() - timedelta(days=1),
    )

    touchpoints.reschedule_service_touchpoints(res)

    sent.refresh_from_db()
    assert sent.status == TouchPoint.Status.CANCELLED
    fresh = TouchPoint.objects.get(
        reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER, status=TouchPoint.Status.SCHEDULED
    )
    assert fresh.scheduled_for == res.pickup_at - timedelta(hours=72)


def test_reschedule_clears_a_stale_customer_and_affiliate_acknowledgement():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(), customer_confirmed_at=timezone.now())
    a = AssignmentFactory(
        reservation=res, status=Assignment.Status.CONFIRMED, affiliate_confirmed_at=timezone.now()
    )

    touchpoints.reschedule_service_touchpoints(res)

    res.refresh_from_db()
    a.refresh_from_db()
    assert res.customer_confirmed_at is None
    assert a.affiliate_confirmed_at is None


def test_mark_lost_cancels_reservation_touchpoints(client):
    from django.urls import reverse

    from apps.accounts.factories import UserFactory

    lead = LeadFactory(status=Lead.Status.NEW)
    res = ReservationFactory(lead=lead, pickup_date=_future())
    tp = TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        scheduled_for=timezone.now() + timedelta(days=1),
    )
    client.force_login(UserFactory())
    client.post(reverse("lead_mark_lost", args=[lead.pk]), {"reason": "x"})
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.CANCELLED


def test_book_lead_cancel_pending_spares_reservation_kinds():
    lead = LeadFactory(status=Lead.Status.NEW)
    res = ReservationFactory(lead=lead, pickup_date=_future())
    TouchPoint.objects.create(
        lead=lead, kind=K.TP1_WELCOME, scheduled_for=timezone.now() + timedelta(hours=1)
    )
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        from apps.leads.services import book_lead

        book_lead(lead)
    welcome = TouchPoint.objects.get(lead=lead, kind=K.TP1_WELCOME)
    assert welcome.status == TouchPoint.Status.CANCELLED
    assert TouchPoint.objects.filter(
        reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER, status=TouchPoint.Status.SCHEDULED
    ).exists()


# --- affiliate confirmation (APC-20) -------------------------------------------------


def test_confirming_an_affiliate_schedules_the_t48h_confirmation():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.OFFERED)

    dispatch_services.confirm(a)

    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_AFFILIATE)
    assert tp.scheduled_for == res.pickup_at - timedelta(hours=48)


def test_withdrawing_confirmed_coverage_cancels_the_affiliate_confirmation():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.OFFERED)
    dispatch_services.confirm(a)
    dispatch_services.withdraw(a)
    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_AFFILIATE)
    assert tp.status == TouchPoint.Status.CANCELLED


def test_in_house_assignment_gets_no_affiliate_confirmation():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    AssignmentFactory(reservation=res, in_house=True)
    assert not TouchPoint.objects.filter(kind=K.TRIP_CONFIRM_AFFILIATE).exists()


# --- triggered kinds (APC-21 / APC-22) ---------------------------------------------


def test_status_change_creates_a_message_only_when_enabled():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())

    set_trip_status(res, "dispatched")
    assert not TouchPoint.objects.filter(kind=K.STATUS_DISPATCHED).exists()

    cfg = NotificationConfig.load()
    cfg.status_dispatched_enabled = True
    cfg.save()
    set_trip_status(res, "on_the_way")  # change again so the event fires
    set_trip_status(res, "dispatched")
    tp = TouchPoint.objects.get(reservation=res, kind=K.STATUS_DISPATCHED)
    assert tp.scheduled_for <= timezone.now()


def test_trigger_driver_released_needs_confirmed_plus_driver_info():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    a = AssignmentFactory(
        reservation=res,
        status=Assignment.Status.CONFIRMED,
        driver_name="",
        driver_cell="",
    )
    touchpoints.trigger_driver_released(a)
    assert not TouchPoint.objects.filter(kind=K.DRIVER_RELEASED).exists()

    a.driver_name = "Sam Rivera"
    a.driver_cell = "+15715551212"
    a.save()
    touchpoints.trigger_driver_released(a)
    assert TouchPoint.objects.filter(reservation=res, kind=K.DRIVER_RELEASED).count() == 1
    touchpoints.trigger_driver_released(a)  # once only
    assert TouchPoint.objects.filter(reservation=res, kind=K.DRIVER_RELEASED).count() == 1


# --- _process skip logic + send ---------------------------------------------------


def _due(tp):
    tp.scheduled_for = timezone.now() - timedelta(minutes=1)
    tp.save(update_fields=["scheduled_for"])


@override_settings(PUBLIC_BASE_URL="https://apc.example.com")
def test_customer_confirmation_sends_for_a_booked_lead():
    lead = _booked_lead()
    lead.contact.phone = "+15715551212"
    lead.contact.email = "rider@example.com"
    lead.contact.save()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    touchpoints.schedule_service_touchpoints(lead)
    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_CUSTOMER)
    _due(tp)

    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}) as send:
        assert touchpoints._process(tp) is True
    body = send.call_args.kwargs["body"]
    assert "https://apc.example.com/trip/" in body


def test_reservation_kind_skips_when_lead_not_booked():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    res = ReservationFactory(lead=lead, pickup_date=_future())
    tp = TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert touchpoints._process(tp) is False
    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


def test_reservation_kind_skips_a_cancelled_trip():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future(), trip_status="cancelled")
    tp = TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    assert touchpoints._process(tp) is False
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


@override_settings(PUBLIC_BASE_URL="https://apc.example.com")
def test_reservation_kind_skips_when_the_message_is_disabled():
    cfg = NotificationConfig.load()
    cfg.trip_confirm_customer_enabled = False
    cfg.save()
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    tp = TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    assert touchpoints._process(tp) is False
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


@override_settings(PUBLIC_BASE_URL="https://apc.example.com")
def test_affiliate_confirmation_goes_to_the_vendor_contact():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.OFFERED)
    a.vendor.email = "dispatch@affiliate.test"
    a.vendor.phone = ""
    a.vendor.save()
    dispatch_services.confirm(a)
    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_AFFILIATE)
    _due(tp)

    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}) as send:
        assert touchpoints._process(tp) is True
    assert send.call_args.kwargs["identifier"] == "dispatch@affiliate.test"


def test_affiliate_confirmation_skips_when_coverage_was_dropped():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.OFFERED)
    dispatch_services.confirm(a)
    tp = TouchPoint.objects.get(reservation=res, kind=K.TRIP_CONFIRM_AFFILIATE)
    # simulate the row surviving but coverage gone (race between schedule + send)
    tp.status = TouchPoint.Status.SCHEDULED
    _due(tp)
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status"])
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert touchpoints._process(tp) is False
    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


def test_lead_level_kind_still_skips_a_booked_lead():
    lead = _booked_lead()
    tp = TouchPoint.objects.create(
        lead=lead,
        kind=K.TP1_WELCOME,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    assert touchpoints._process(tp) is False
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


# --- build_context -------------------------------------------------------------------


def test_build_context_fills_trip_vars_from_the_reservation():
    from apps.messaging.touchpoint_templates import build_context

    lead = _booked_lead()
    res = ReservationFactory(
        lead=lead,
        pickup_date=_future(),
        pickup_time=None,
        passengers=7,
        stops=["Dulles Airport", "The Ritz-Carlton"],
    )
    ctx = build_context(lead, res)
    assert ctx["trip_passengers"] == "7"
    assert "Dulles Airport" in ctx["trip_routing"]
    assert "The Ritz-Carlton" in ctx["trip_routing"]
    assert ctx["trip_pickup_date"]
    # No pickup_time on the trip: trip_pickup_time stays blank, and the combined var
    # (what the templates actually interpolate) drops the "at" clause rather than
    # rendering a dangling "... at ".
    assert ctx["trip_pickup_time"] == ""
    assert ctx["trip_pickup_when"] == ctx["trip_pickup_date"]
    # keys are always present even with no reservation
    assert build_context(lead)["trip_routing"] == ""


def test_build_context_time_carries_the_trip_timezone_abbreviation():
    """CLAUDE.md: a viewer must see the abbreviation whenever a trip's zone differs from
    the project's own (`pickup_tz_abbrev` is blank in-zone by design — nothing to add)."""
    from datetime import time

    from apps.messaging.touchpoint_templates import build_context

    lead = _booked_lead()
    res = ReservationFactory(
        lead=lead,
        pickup_date=_future(),
        pickup_time=time(14, 0),
        pickup_timezone="America/Los_Angeles",  # differs from settings.TIME_ZONE
    )
    ctx = build_context(lead, res)
    assert ctx["trip_pickup_time"].endswith(("PDT", "PST"))
    assert ctx["trip_pickup_when"] == f"{ctx['trip_pickup_date']} at {ctx['trip_pickup_time']}"


def test_run_touchpoints_disabled_in_dev_is_a_noop():
    lead = _booked_lead()
    res = ReservationFactory(lead=lead, pickup_date=_future())
    TouchPoint.objects.create(
        lead=lead,
        reservation=res,
        kind=K.TRIP_CONFIRM_CUSTOMER,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    assert touchpoints.run_touchpoints() == 0
