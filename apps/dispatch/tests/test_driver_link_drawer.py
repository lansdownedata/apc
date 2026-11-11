"""The trip drawer's driver link controls and the passenger-info setting (Task 9)."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import services
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.sender import SendResult
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

NO_EMAIL = "No email on file — copy the driver link"
NO_ADDRESS = "No mobile or email on file — copy the driver link"


def _trip():
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=timezone.localdate() + timedelta(days=21),
        pickup_time=time(6, 15),
    )


def _panel(client, trip) -> str:
    return client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()


def _confirmed(commit, vendor=None):
    with commit(execute=True):
        return services.assign_direct(
            _trip(), vendor or VendorFactory(email="ops@capital.example"), payout=Decimal("1")
        )


def test_drawer_shows_copy_and_resend_on_confirmed_coverage(
    logged_in_client, django_capture_on_commit_callbacks
):
    assignment = _confirmed(django_capture_on_commit_callbacks)

    body = _panel(logged_in_client, assignment.reservation)

    assert "Copy driver link" in body
    assert "Resend link" in body
    assert reverse("dispatch_resend_driver_link", args=[assignment.pk]) in body
    assert "/trip/driver/" in body
    assert "Link sent" in body or "Not sent" in body


def test_drawer_hides_them_on_an_offer(logged_in_client):
    trip = _trip()
    Assignment.objects.create(
        reservation=trip,
        vendor=VendorFactory(),
        payout=Decimal("1"),
        status=Assignment.Status.OFFERED,
    )

    body = _panel(logged_in_client, trip)

    assert "Copy driver link" not in body
    assert "Resend link" not in body


def test_no_email_line_for_vendor_without_email(
    logged_in_client, django_capture_on_commit_callbacks
):
    assignment = _confirmed(django_capture_on_commit_callbacks, VendorFactory(email=""))

    body = _panel(logged_in_client, assignment.reservation)

    assert NO_EMAIL in body
    assert "Copy driver link" in body
    assert "Resend link" not in body


def test_no_address_line_for_in_house_driver(logged_in_client):
    trip = _trip()
    services.assign_in_house(trip, DriverFactory(phone="", email=""))

    assert NO_ADDRESS in _panel(logged_in_client, trip)


def test_resend_calls_send_now_again(logged_in_client, django_capture_on_commit_callbacks):
    assignment = _confirmed(django_capture_on_commit_callbacks)

    with patch("apps.messaging.sender.send_now") as send_now:
        send_now.return_value = SendResult(sent=True, deliveries=[], errors=[])
        resp = logged_in_client.post(reverse("dispatch_resend_driver_link", args=[assignment.pk]))

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert send_now.call_count == 1
    assert send_now.call_args.args[0] == "affiliate_confirmed"
    assert "/trip/driver/" in send_now.call_args.kwargs["extra_ctx"]["driver_link"]


def test_resend_refused_for_offered_assignment(logged_in_client):
    offered = Assignment.objects.create(
        reservation=_trip(),
        vendor=VendorFactory(),
        payout=Decimal("1"),
        status=Assignment.Status.OFFERED,
    )

    with patch("apps.messaging.sender.send_now") as send_now:
        resp = logged_in_client.post(reverse("dispatch_resend_driver_link", args=[offered.pk]))

    assert resp.status_code == 400
    send_now.assert_not_called()


def test_resend_needs_a_login_and_a_post(logged_in_client):
    url = reverse("dispatch_resend_driver_link", args=[1])
    anonymous = Client()
    assert anonymous.post(url).status_code == 302
    assert logged_in_client.get(url).status_code == 405


def test_resend_reports_a_failed_send(
    logged_in_client, django_capture_on_commit_callbacks, settings
):
    assignment = _confirmed(django_capture_on_commit_callbacks)
    settings.OUTBOUND_ENABLED = False

    resp = logged_in_client.post(reverse("dispatch_resend_driver_link", args=[assignment.pk]))

    assert resp.json() == {"ok": False, "state": "Not sent"}


def test_settings_form_saves_passenger_setting(client):
    client.force_login(UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN))
    page = client.get(reverse("dispatch_alerts"))
    assert "Show passenger info to vendor drivers" in page.content.decode()

    form = page.context["form"]
    data = {
        name: ("on" if value is True else "" if value in (None, False) else value)
        for name, value in form.initial.items()
        if name in form.fields
    }
    data.pop("driver_page_show_passenger_contact", None)
    resp = client.post(reverse("dispatch_alerts"), data)

    assert resp.status_code == 302
    assert DispatchAlertConfig.load().driver_page_show_passenger_contact is False


def _kill_link(assignment):
    """Done more than 12 hours ago: the link is dead."""
    from apps.reservations.models import Reservation, TripStatusEvent
    from apps.reservations.services import set_trip_status

    set_trip_status(assignment.reservation, Reservation.TripStatus.DONE)
    TripStatusEvent.objects.filter(reservation=assignment.reservation).update(
        created_at=timezone.now() - timedelta(hours=13)
    )


def test_dead_link_hides_copy_and_resend(logged_in_client, django_capture_on_commit_callbacks):
    assignment = _confirmed(django_capture_on_commit_callbacks)
    _kill_link(assignment)

    body = _panel(logged_in_client, assignment.reservation)

    assert "Copy driver link" not in body
    assert "Resend link" not in body


def test_resend_refused_for_a_dead_link(logged_in_client, django_capture_on_commit_callbacks):
    assignment = _confirmed(django_capture_on_commit_callbacks)
    _kill_link(assignment)

    with patch("apps.messaging.sender.send_now") as send_now:
        resp = logged_in_client.post(reverse("dispatch_resend_driver_link", args=[assignment.pk]))

    assert resp.status_code == 400
    send_now.assert_not_called()
