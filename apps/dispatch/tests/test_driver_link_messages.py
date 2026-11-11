"""The driver link reaches the driver (spec "Who gets the link"): the vendor's "Trip
confirmed" email on drawer confirm, GNet accept and a phone-arranged assign; the in-house
driver's "Driver trip assignment" text; `{driver_link}` in the T-48h affiliate confirmation.
"""

import json
import re
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import driver_link, gnet_callback, services
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import fields, listing, rules
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.notifications.models import Notification
from apps.reservations.acknowledgements import read_driver_link_token
from apps.reservations.factories import ReservationFactory
from apps.settings.tests.test_message_editor import post_data
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

PODIUM = "apps.integrations.podium.send_message"
LINK = "/trip/driver/"


def _pickup():
    """Three weeks out, so no customer driver notice is due; Phoenix keeps MST all year."""
    return timezone.localdate() + timedelta(days=21)


def _trip():
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=_pickup(),
        pickup_time=time(6, 15),
        pickup_timezone="America/Phoenix",
        rate=285,
        hours=1,
    )


def _vendor(**kw):
    kw.setdefault("name", "Capital Chauffeurs")
    kw.setdefault("email", "ops@capital.example")
    return VendorFactory(**kw)


def _offered(vendor=None, **kw):
    trip = _trip()
    return Assignment.objects.create(
        reservation=trip,
        vendor=vendor or _vendor(),
        payout=Decimal("215.00"),
        status=Assignment.Status.OFFERED,
        **kw,
    )


def _confirmed(**kw):
    assignment = _offered(**kw)
    Assignment.objects.filter(pk=assignment.pk).update(status=Assignment.Status.CONFIRMED)
    assignment.refresh_from_db()
    return assignment


def _linked(text: str, base_url: str = "") -> Assignment:
    """The coverage the driver link in ``text`` opens. The token is time-stamped, so two
    links to one assignment can differ; what matters is whose it is."""
    match = re.search(re.escape(base_url + LINK) + r"([^/\s]+)/", text)
    assert match, f"no driver link in {text!r}"
    return read_driver_link_token(match.group(1))


def _trip_confirmed(mailoutbox):
    return [m for m in mailoutbox if m.subject.startswith("Trip confirmed")]


# --- the vendor's "Trip confirmed" email ------------------------------------------------


def test_drawer_confirm_sends_trip_confirmed_with_the_link(
    mailoutbox, django_capture_on_commit_callbacks
):
    assignment = _offered()
    with django_capture_on_commit_callbacks(execute=True):
        services.confirm(assignment)

    [message] = _trip_confirmed(mailoutbox)
    assert message.to == ["ops@capital.example"]
    assert message.subject == f"Trip confirmed — {_pickup():%b %-d}"
    assert _linked(message.body) == assignment
    assert "Capital Chauffeurs — trip confirmed with" in message.body
    assert "Forward this link to your driver." in message.body
    assert "6:15 AM MST" in message.body


def test_gnet_accept_sends_trip_confirmed(mailoutbox, django_capture_on_commit_callbacks):
    assignment = _offered(vendor=_vendor(gnet_grid_id="gnet-1"), channel=Assignment.Channel.GNET)
    with django_capture_on_commit_callbacks(execute=True):
        gnet_callback._apply_status(assignment, "CONFIRMED", {})

    [message] = _trip_confirmed(mailoutbox)
    assert LINK in message.body


def test_assign_direct_sends_trip_confirmed(mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assignment = services.assign_direct(_trip(), _vendor(), payout=Decimal("215.00"))

    [message] = _trip_confirmed(mailoutbox)
    assert _linked(message.body) == assignment
    assert not [m for m in mailoutbox if m.subject.startswith("Trip offer")]


def test_nothing_is_sent_before_the_assignment_commits(mailoutbox):
    services.assign_direct(_trip(), _vendor(), payout=Decimal("215.00"))
    assert mailoutbox == []


# --- the in-house driver's text ---------------------------------------------------------


def test_assign_in_house_texts_the_driver_not_the_vendor_row(
    mailoutbox, django_capture_on_commit_callbacks
):
    trip = _trip()
    driver = DriverFactory(name="Sam Driver", phone="+15715550142", email="sam@apc.example")
    with patch(PODIUM, return_value={"uid": "m1"}) as podium:
        with django_capture_on_commit_callbacks(execute=True):
            assignment = services.assign_in_house(trip, driver)

    [call] = [c for c in podium.call_args_list if c.kwargs["identifier"] == "+15715550142"]
    body = call.kwargs["body"]
    assert _linked(body) == assignment
    day = f"{_pickup():%b %-d, %Y}"
    assert f"you're driving trip #{trip.reference} on {day} at 6:15 AM MST" in body
    [email] = [m for m in mailoutbox if m.to == ["sam@apc.example"]]
    assert email.subject == f"Trip #{trip.reference} on {day}"
    assert not TouchPoint.objects.filter(kind="affiliate_confirmed").exists()
    assert TouchPoint.objects.get(kind="driver_assignment").status == TouchPoint.Status.SENT


def test_reassign_in_house_texts_the_new_driver_with_a_new_link(
    django_capture_on_commit_callbacks,
):
    trip = _trip()
    first = DriverFactory(phone="+15715550101", email="")
    second = DriverFactory(phone="+15715550102", email="")
    with patch(PODIUM, return_value={"uid": "m1"}) as podium:
        with django_capture_on_commit_callbacks(execute=True):
            old = services.assign_in_house(trip, first)
        services.withdraw(old, note="sick")
        with django_capture_on_commit_callbacks(execute=True):
            new = services.assign_in_house(trip, second)

    drivers = ("+15715550101", "+15715550102")
    sent = [
        (c.kwargs["identifier"], c.kwargs["body"])
        for c in podium.call_args_list
        if c.kwargs["identifier"] in drivers
    ]
    assert [to for to, _ in sent] == list(drivers)
    assert _linked(sent[0][1]) == old
    assert _linked(sent[1][1]) == new != old


# --- a send that doesn't go never fails the assignment -----------------------------------


def test_outbound_off_not_sent_notifies_and_keeps_assignment(
    settings, mailoutbox, django_capture_on_commit_callbacks
):
    settings.OUTBOUND_ENABLED = False
    trip = _trip()
    with django_capture_on_commit_callbacks(execute=True):
        assignment = services.assign_direct(trip, _vendor(), payout=Decimal("215.00"))

    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED
    assert mailoutbox == []
    note = Notification.objects.get(lead=trip.lead, kind=Notification.Kind.SYNC_FAILED)
    assert note.title == "Driver link to Capital Chauffeurs not sent"
    assert note.detail == f"Trip #{trip.reference}: copy the driver link from the drawer."


def test_a_send_that_raises_is_logged_and_notified(django_capture_on_commit_callbacks):
    trip = _trip()
    with patch("apps.messaging.sender.send_now", side_effect=RuntimeError("boom")):
        with django_capture_on_commit_callbacks(execute=True):
            assignment = services.assign_direct(trip, _vendor(), payout=Decimal("215.00"))

    assert assignment.status == Assignment.Status.CONFIRMED
    assert Notification.objects.filter(lead=trip.lead).count() == 1


def test_vendor_without_email_no_send_no_notification(
    mailoutbox, django_capture_on_commit_callbacks
):
    trip = _trip()
    with django_capture_on_commit_callbacks(execute=True):
        assignment = services.assign_direct(trip, _vendor(email=""), payout=Decimal("215.00"))

    assert driver_link.send_link(assignment) is None
    assert mailoutbox == []
    assert not TouchPoint.objects.filter(reservation=trip, kind="affiliate_confirmed").exists()
    assert not Notification.objects.filter(lead=trip.lead).exists()


def test_driver_without_phone_or_email_no_send_no_notification(
    django_capture_on_commit_callbacks,
):
    trip = _trip()
    with patch(PODIUM) as podium:
        with django_capture_on_commit_callbacks(execute=True):
            services.assign_in_house(trip, DriverFactory(phone="", email=""))

    assert not TouchPoint.objects.filter(reservation=trip, kind="driver_assignment").exists()
    assert not podium.called
    assert not Notification.objects.filter(lead=trip.lead).exists()


# --- the T-48h affiliate confirmation ----------------------------------------------------


def test_t48_affiliate_row_fills_driver_link():
    trip = _trip()
    assignment = Assignment.objects.create(
        reservation=trip, vendor=_vendor(), payout=Decimal("1"), status="confirmed"
    )
    row = MessageTemplate.objects.get(key="trip_confirm_affiliate")
    tp = TouchPoint(lead=trip.lead, reservation=trip, message=row, kind=row.key)
    ctx: dict = {}

    rules.fill_links(tp, row, ctx, "https://portal.example.com")

    assert _linked(ctx["driver_link"], "https://portal.example.com") == assignment


def test_t48_affiliate_row_offers_driver_link_in_the_editor():
    keys = {f.key for f in fields.fields_for("pickup", "affiliate", "trip_confirm_affiliate")}
    assert "driver_link" in keys
    assert "driver_link" not in {f.key for f in fields.fields_for("pickup", "affiliate")}


# --- the seeded rows --------------------------------------------------------------------


def test_seeded_rows_exist_with_audiences():
    vendor = MessageTemplate.objects.get(key="affiliate_confirmed")
    assert (vendor.name, vendor.section, vendor.audience, vendor.trigger) == (
        "Trip confirmed",
        "affiliates",
        "affiliate",
        "coverage_confirmed",
    )
    assert (vendor.send_email, vendor.send_sms, vendor.recipients) == (True, False, ["company"])
    assert vendor.seeded and vendor.enabled

    driver = MessageTemplate.objects.get(key="driver_assignment")
    assert (driver.name, driver.section, driver.audience, driver.trigger) == (
        "Driver trip assignment",
        "affiliates",
        MessageTemplate.Audience.DRIVER,
        "driver_assigned",
    )
    assert (driver.send_email, driver.send_sms, driver.recipients) == (
        True,
        True,
        ["in_house_driver"],
    )
    assert driver.seeded and driver.enabled


@pytest.mark.parametrize("key", ["affiliate_confirmed", "driver_assignment"])
def test_seeded_rows_validate_and_show_no_problem(key):
    row = MessageTemplate.objects.get(key=key)
    assert fields.validate(listing.row_data(row)) == {}
    assert listing.problems(row) == []


# --- what the drawer reads --------------------------------------------------------------


def test_link_send_state(settings, mailoutbox, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        sent = services.assign_direct(_trip(), _vendor(), payout=Decimal("1"))
    assert driver_link.link_send_state(sent) == "sent"

    settings.OUTBOUND_ENABLED = False
    with django_capture_on_commit_callbacks(execute=True):
        refused = services.assign_direct(_trip(), _vendor(), payout=Decimal("1"))
    assert driver_link.link_send_state(refused) == "not_sent"

    settings.OUTBOUND_ENABLED = True
    with django_capture_on_commit_callbacks(execute=True):
        nowhere = services.assign_direct(_trip(), _vendor(email=""), payout=Decimal("1"))
    assert driver_link.link_send_state(nowhere) == "no_address"

    in_house = services.assign_in_house(_trip(), DriverFactory(phone="", email=""))
    assert driver_link.link_send_state(in_house) == "no_address"

    assert driver_link.link_send_state(_offered()) == ""


def test_the_driver_row_is_offered_no_money_and_no_customer_links():
    keys = {f.key for f in fields.fields_for("driver_assigned", "driver", "driver_assignment")}
    assert {"driver_link", "trip_ref", "trip_pickup_date", "trip_pickup_time"} <= keys
    for money_or_link in ("payout", "balance_amount", "quote_breakdown", "pay_link", "quote_link"):
        assert money_or_link not in keys


@pytest.mark.parametrize("key", ["affiliate_confirmed", "driver_assignment"])
def test_the_rows_open_in_the_settings_editor_and_save_unchanged(client, key):
    client.force_login(UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN))
    row = MessageTemplate.objects.get(key=key)

    assert client.get(reverse("message_edit", args=[key])).status_code == 200
    resp = client.post(reverse("message_edit", args=[key]), post_data(row))
    assert resp.status_code == 302, resp.content.decode()[:2000]


def test_the_driver_row_previews_against_an_order_with_our_driver(client):
    client.force_login(UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN))
    trip = _trip()
    services.assign_in_house(trip, DriverFactory(name="Sam Driver", phone="+15715550142"))
    row = MessageTemplate.objects.get(key="driver_assignment")

    resp = client.post(
        reverse("message_preview", args=["driver_assignment"]), post_data(row, lead=trip.lead.pk)
    )

    body = json.loads(resp.content)
    assert body["ok"] is True
    assert LINK in body["sms"] and trip.reference in body["sms"]
    assert "Sam Driver (in-house driver) · text and email" in body["to"]


# --- send_link never raises, and respects admin choices ----------------------------------


def test_send_link_swallows_a_url_that_cannot_be_built():
    assignment = _confirmed()
    with patch("apps.reservations.acknowledgements.driver_link_url", side_effect=RuntimeError):
        result = driver_link.send_link(assignment)
    assert result.sent is False


def test_send_link_swallows_a_notification_that_cannot_be_written(settings):
    settings.OUTBOUND_ENABLED = False
    assignment = _confirmed()
    with patch.object(Notification, "notify", side_effect=RuntimeError):
        assert driver_link.send_link(assignment).sent is False


def test_a_row_turned_off_in_settings_raises_no_notification(mailoutbox):
    MessageTemplate.objects.filter(key="affiliate_confirmed").update(enabled=False)
    assignment = _confirmed()

    result = driver_link.send_link(assignment)

    assert result.sent is False
    assert not Notification.objects.filter(title__startswith="Driver link to").exists()
    assert driver_link.link_send_state(assignment) == "not_sent"


def test_a_row_limited_to_other_service_types_raises_no_notification(mailoutbox):
    from apps.leads.models import ServiceType

    row = MessageTemplate.objects.get(key="affiliate_confirmed")
    row.service_types.add(ServiceType.objects.create(name="Only this one"))
    assignment = _confirmed()

    result = driver_link.send_link(assignment)

    assert result.sent is False
    assert not Notification.objects.filter(title__startswith="Driver link to").exists()
    assert driver_link.link_send_state(assignment) == "not_sent"


def test_a_refused_send_still_raises_a_notification(settings):
    settings.OUTBOUND_ENABLED = False
    driver_link.send_link(_confirmed())
    assert Notification.objects.filter(title__startswith="Driver link to").exists()


def test_a_send_still_in_flight_reads_as_nothing_yet():
    assignment = _confirmed()
    TouchPoint.objects.create(
        lead=assignment.reservation.lead,
        reservation=assignment.reservation,
        kind="affiliate_confirmed",
        status=TouchPoint.Status.SCHEDULED,
        scheduled_for=timezone.now(),
    )
    assert driver_link.link_send_state(assignment) == ""


def test_the_vendor_row_is_offered_no_payout_and_no_customer_links():
    keys = {
        f.key for f in fields.fields_for("coverage_confirmed", "affiliate", "affiliate_confirmed")
    }
    assert {"driver_link", "vendor_name", "trip_day", "trip_sheet"} <= keys
    for money_or_link in ("payout", "pay_link", "quote_link", "balance_amount"):
        assert money_or_link not in keys
