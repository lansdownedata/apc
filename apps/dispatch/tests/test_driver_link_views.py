"""Driver trip link: the public page and its JSON endpoints."""

import json
from datetime import date, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig, DriverPing
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.acknowledgements import driver_link_url, make_driver_link_token
from apps.reservations.factories import ReservationFactory, StopFactory
from apps.reservations.models import Passenger, Reservation
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"


def _soon(zone="America/New_York"):
    """Pickup one hour from now in the trip's zone, so the 6-hour window is always open."""
    local = (timezone.now() + timedelta(hours=1)).astimezone(ZoneInfo(zone))
    return {"pickup_date": local.date(), "pickup_time": local.time(), "pickup_timezone": zone}


def _live(status=S.DISPATCHED, **trip_kw):
    if "pickup_date" not in trip_kw:
        trip_kw = {**_soon(trip_kw.get("pickup_timezone") or "America/New_York"), **trip_kw}
    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), **trip_kw)
    assignment = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    set_trip_status(trip, status)
    return assignment


def _place_stops(assignment, *points):
    """Give the trip's factory-made stops coordinates, in order."""
    for stop, (lat, lng) in zip(assignment.reservation.ordered_stops, points, strict=True):
        stop.latitude, stop.longitude = lat, lng
        stop.save(update_fields=["latitude", "longitude"])


def _url(assignment, name="driver_link"):
    token = make_driver_link_token(assignment)
    return reverse(name, args=[token])


def _post(client, assignment, name, body=None, **kw):
    return client.post(
        _url(assignment, name), json.dumps(body or {}), content_type="application/json", **kw
    )


def test_driver_link_url_resolves(client):
    a = _live()
    assert driver_link_url(a) == reverse("driver_link", args=[make_driver_link_token(a)])
    assert client.get(driver_link_url(a)).status_code == 200


def test_page_renders_for_a_live_link(client):
    a = _live()
    res = client.get(_url(a))
    assert res.status_code == 200
    assert a.reservation.reference in res.content.decode()
    assert res["Cache-Control"] == "private, no-store"
    assert res["Referrer-Policy"] == "no-referrer"
    assert res["X-Robots-Tag"] == "noindex"


@pytest.mark.parametrize(
    "name",
    ["driver_link_status", "driver_link_circling", "driver_link_no_show", "driver_link_ping"],
)
def test_bad_token_is_404(client, name):
    assert client.get(reverse("driver_link", args=["junk"])).status_code == 404
    assert (
        client.post(reverse(name, args=["junk"]), "{}", content_type="application/json").status_code
        == 404
    )


def test_dead_link_page_shows_only_the_phone_line(client):
    a = _live(status=S.DISPATCHED)
    StopFactory(reservation=a.reservation, sequence=1, address="1 Secret Pickup Way")
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status"])
    with override_settings(COMPANY_PHONE="555-0100"):
        res = client.get(_url(a))
    html = res.content.decode()
    assert res.status_code == 200
    assert "This trip is no longer assigned to you. Call 555-0100 with questions." in html
    assert "1 Secret Pickup Way" not in html
    assert a.reservation.reference not in html
    assert res["Cache-Control"] == "private, no-store"


def test_dead_link_page_without_a_company_phone_says_dispatch(client):
    a = _live(status=S.DISPATCHED)
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status"])
    with override_settings(COMPANY_PHONE=""):
        html = client.get(_url(a)).content.decode()
    assert "This trip is no longer assigned to you. Call dispatch with questions." in html
    assert "Call  with" not in html
    assert "tel:" not in html


def test_post_to_dead_link_is_410(client):
    a = _live()
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status"])
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert res.status_code == 410
    assert res.json() == {"dead": True}


def test_status_post_advances_and_returns_state(client):
    a = _live()
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert res.status_code == 200
    state = res.json()["state"]
    assert state["status"] == S.ON_THE_WAY
    assert state["next"] == {"to": S.ARRIVED, "label": "I've arrived"}
    assert state["share_location"] is True
    a.reservation.refresh_from_db()
    assert a.reservation.trip_status == S.ON_THE_WAY


def test_status_post_with_top_level_coords_writes_one_status_ping(client):
    a = _live()
    res = _post(
        client,
        a,
        "driver_link_status",
        {"to": S.ON_THE_WAY, "from": S.DISPATCHED, "lat": 40.7, "lng": -74.0, "accuracy": 9},
    )
    assert res.status_code == 200
    ping = DriverPing.objects.get()
    assert ping.kind == DriverPing.Kind.STATUS
    assert ping.status_event is not None and ping.status_event.status == S.ON_THE_WAY


def test_status_post_without_coords_writes_no_ping(client):
    a = _live()
    _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert not DriverPing.objects.exists()


def test_stale_from_is_409_with_current_state(client):
    a = _live(status=S.ON_THE_WAY)
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert res.status_code == 409
    assert res.json()["state"]["status"] == S.ON_THE_WAY


def test_before_window_is_400_with_opens_at(client):
    a = _live(pickup_date=timezone.localdate() + timedelta(days=3))
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert res.status_code == 400
    body = res.json()
    assert body["error"].startswith("Opens at ")
    assert body["error"].endswith(", 6 hours before pickup")
    assert body["state"]["window_open"] is False


def test_malformed_json_is_400(client):
    a = _live()
    for name in ("driver_link_status", "driver_link_circling", "driver_link_no_show"):
        res = client.post(_url(a, name), "{nope", content_type="application/json")
        assert res.status_code == 400, name
    res = client.post(_url(a, "driver_link_ping"), "{nope", content_type="application/json")
    assert res.status_code == 400


def test_waiting_on_dispatch_when_trip_is_assigned(client):
    a = _live(status=S.ASSIGNED)
    res = client.get(_url(a))
    html = res.content.decode()
    assert "Waiting on dispatch" in html
    state = _post(client, a, "driver_link_no_show").status_code
    assert state == 200
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.ASSIGNED})
    assert res.status_code == 409
    assert res.json()["state"]["message"] == "Waiting on dispatch"
    assert res.json()["state"]["next"] is None


def test_no_pickup_date_says_call_dispatch(client):
    a = _live()
    a.reservation.pickup_date = None
    a.reservation.save(update_fields=["pickup_date"])
    res = _post(client, a, "driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED})
    assert res.status_code == 400
    assert res.json()["state"]["message"] == "Pickup time not set — call dispatch."


def test_times_use_the_trip_zone(client):
    a = _live(pickup_timezone="America/Los_Angeles", pickup_date=date(2026, 1, 20))
    StopFactory(reservation=a.reservation, sequence=1, address="LAX", scheduled_time=time(9, 0))
    html = client.get(_url(a)).content.decode()
    assert "PST" in html
    assert "EST" not in html and "EDT" not in html


def _with_lead_passenger(a):
    Passenger.objects.create(
        reservation=a.reservation, name="Pat Rider", mobile="+15551230000", is_lead=True
    )


def test_vendor_driver_hides_mobile_when_setting_off(client):
    a = _live()
    _with_lead_passenger(a)
    DispatchAlertConfig.objects.all().delete()
    DispatchAlertConfig.objects.create(driver_page_show_passenger_contact=False)
    html = client.get(_url(a)).content.decode()
    assert "+15551230000" not in html
    assert "Pat Rider" in html


def test_in_house_driver_always_sees_mobile(client):
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        **_soon(),
    )
    a = AssignmentFactory(reservation=trip, in_house=True)
    set_trip_status(trip, S.DISPATCHED)
    _with_lead_passenger(a)
    DispatchAlertConfig.objects.all().delete()
    DispatchAlertConfig.objects.create(driver_page_show_passenger_contact=False)
    assert "+15551230000" in client.get(_url(a)).content.decode()


def test_no_public_key_means_no_map_and_no_route_call(client):
    a = _live()
    _place_stops(a, (40.0, -74.0), (40.1, -74.1))
    with (
        override_settings(LOCATIONIQ_PUBLIC_KEY=""),
        patch("apps.dispatch.driver_link_views.route_for") as route,
    ):
        assert client.get(_url(a)).status_code == 200
    route.assert_not_called()


def test_route_loaded_with_public_key_and_coordinates(client):
    a = _live()
    _place_stops(a, (40.0, -74.0), (40.1, -74.1))
    with (
        override_settings(LOCATIONIQ_PUBLIC_KEY="pk.public"),
        patch("apps.dispatch.driver_link_views.route_for", return_value="abc123") as route,
    ):
        html = client.get(_url(a)).content.decode()
    route.assert_called_once()
    assert "abc123" in html and "pk.public" in html


def test_server_key_never_rendered(client):
    a = _live()
    with override_settings(LOCATIONIQ_API_KEY="srv-secret", LOCATIONIQ_PUBLIC_KEY="pk.public"):
        html = client.get(_url(a)).content.decode()
    assert "srv-secret" not in html


def test_page_shows_no_money(client):
    a = _live()
    html = client.get(_url(a)).content.decode().lower()
    for word in ("payout", "price", "$140", "total"):
        assert word not in html


def test_ping_204_and_saved(client):
    a = _live(status=S.ON_THE_WAY)
    res = _post(
        client,
        a,
        "driver_link_ping",
        {"lat": 40.7, "lng": -74.0, "accuracy": 8, "recorded_at": "2026-11-12T12:00:00Z"},
    )
    assert res.status_code == 204
    ping = DriverPing.objects.get()
    assert ping.recorded_at is not None and ping.recorded_at.tzinfo is not None


def test_ping_junk_recorded_at_is_stored_as_none(client):
    a = _live(status=S.ON_THE_WAY)
    res = _post(client, a, "driver_link_ping", {"lat": 40.7, "lng": -74.0, "recorded_at": "junk"})
    assert res.status_code == 204
    assert DriverPing.objects.get().recorded_at is None


def test_ping_naive_recorded_at_is_stored_as_none(client):
    a = _live(status=S.ON_THE_WAY)
    res = _post(
        client,
        a,
        "driver_link_ping",
        {"lat": 40.7, "lng": -74.0, "recorded_at": "2026-11-12T12:00:00"},
    )
    assert res.status_code == 204
    assert DriverPing.objects.get().recorded_at is None


def test_ping_bad_coordinates_400(client):
    a = _live(status=S.ON_THE_WAY)
    res = _post(client, a, "driver_link_ping", {"lat": 999, "lng": 0})
    assert res.status_code == 400
    assert not DriverPing.objects.exists()


def test_ping_on_dead_link_204_nothing_saved(client):
    a = _live(status=S.ON_THE_WAY)
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status"])
    res = _post(client, a, "driver_link_ping", {"lat": 40.7, "lng": -74.0})
    assert res.status_code == 204
    assert not DriverPing.objects.exists()


def test_no_show_post_notifies(client):
    a = _live(status=S.ARRIVED)
    res = _post(client, a, "driver_link_no_show", {"note": "Waited 20 min"})
    assert res.status_code == 200
    assert Notification.objects.filter(kind=Notification.Kind.DRIVER_REPORT).exists()
    a.reservation.refresh_from_db()
    assert a.reservation.driver_no_show_note == "Waited 20 min"


def test_circling_post_toggles(client):
    a = _live(status=S.ARRIVED)
    res = _post(client, a, "driver_link_circling", {"on": True})
    assert res.status_code == 200
    assert res.json()["state"]["circling"] is True
    res = _post(client, a, "driver_link_circling", {"on": False})
    assert res.json()["state"]["circling"] is False


def test_navigate_link_by_user_agent(client):
    a = _live()
    _place_stops(a, (40.5, -74.5), (40.6, -74.6))
    ios = client.get(_url(a), HTTP_USER_AGENT=IPHONE).content.decode()
    assert "https://maps.apple.com/?daddr=40.500000,-74.500000" in ios
    android = client.get(_url(a), HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14)")
    assert "https://www.google.com/maps/dir/?api=1&amp;destination=40.500000,-74.500000" in (
        android.content.decode()
    )


def _csrf_client(assignment):
    """A browser-like client: CSRF enforced, holding the cookie the page set."""
    from django.test import Client

    browser = Client(enforce_csrf_checks=True)
    page = browser.get(_url(assignment))
    assert page.status_code == 200
    return browser, browser.cookies["csrftoken"].value


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("driver_link_status", {"to": S.ON_THE_WAY, "from": S.DISPATCHED}),
        ("driver_link_circling", {"on": True}),
        ("driver_link_no_show", {"note": "x"}),
        ("driver_link_ping", {"lat": 40.7, "lng": -74.0}),
    ],
)
def test_post_with_null_origin_and_no_csrf_token_succeeds(name, body):
    """The signed URL is the only credential, so CSRF is exempt: `Origin: null` (the page's
    no-referrer policy) or a cookie-less in-app browser must not refuse a tap."""
    from django.test import Client

    a = _live(
        status=S.ARRIVED
        if name in ("driver_link_no_show", "driver_link_circling")
        else S.DISPATCHED
    )
    if name == "driver_link_ping":
        set_trip_status(a.reservation, S.ON_THE_WAY)
    browser = Client(enforce_csrf_checks=True)
    res = browser.post(
        _url(a, name), json.dumps(body), content_type="application/json", HTTP_ORIGIN="null"
    )
    assert res.status_code in (200, 204)


def test_no_driver_link_view_reads_request_user():
    """The token is the only authority: nothing here may start trusting a session."""
    from pathlib import Path

    from apps.dispatch import driver_link_views

    assert "request.user" not in Path(driver_link_views.__file__).read_text()


def test_deleted_assignment_is_not_a_404(client):
    a = _live(status=S.ON_THE_WAY)
    token = make_driver_link_token(a)
    a.delete()
    page = client.get(reverse("driver_link", args=[token]))
    assert page.status_code == 200
    assert "no longer assigned" in page.content.decode()
    assert page["Cache-Control"] == "private, no-store"
    for name in ("driver_link_status", "driver_link_circling", "driver_link_no_show"):
        res = client.post(reverse(name, args=[token]), "{}", content_type="application/json")
        assert res.status_code == 410
        assert res.json() == {"dead": True}
    ping = client.post(
        reverse("driver_link_ping", args=[token]), "{}", content_type="application/json"
    )
    assert ping.status_code == 204


def test_page_script_sends_its_origin_on_every_post():
    """Every fetch in the page script overrides the page's no-referrer policy."""
    from pathlib import Path

    from django.conf import settings

    script = (Path(settings.BASE_DIR) / "static" / "js" / "driver_page.js").read_text()
    fetches = script.count("fetch(")
    assert fetches >= 2
    assert script.count('referrerPolicy: "same-origin"') == fetches


def test_page_state_carries_opens_at_in_utc():
    from apps.dispatch.driver_link_views import page_state

    a = _live(
        pickup_date=date(2026, 7, 4), pickup_time=time(9, 0), pickup_timezone="America/New_York"
    )
    assert page_state(a)["opens_at"] == "2026-07-04T07:00:00Z"
    a.reservation.pickup_date = None
    a.reservation.save(update_fields=["pickup_date"])
    assert page_state(a)["opens_at"] is None


def test_lock_returns_freshly_locked_rows():
    from apps.dispatch import driver_link

    a = _live(status=S.DISPATCHED)
    Reservation.objects.filter(pk=a.reservation_id).update(trip_status=S.ON_THE_WAY)
    Assignment.objects.filter(pk=a.pk).update(status=Assignment.Status.WITHDRAWN)
    from django.db import transaction

    with transaction.atomic():
        assignment, trip = driver_link._lock(a.pk)
    assert trip.trip_status == S.ON_THE_WAY
    assert assignment.status == Assignment.Status.WITHDRAWN
    assert assignment.reservation is trip


def test_open_timer_reloads_at_most_once_per_opens_at():
    from pathlib import Path

    from django.conf import settings

    script = (Path(settings.BASE_DIR) / "static" / "js" / "driver_page.js").read_text()
    assert '"driverOpenReload:" + this.state.opens_at' in script
    assert "sessionStorage.getItem(key)" in script
    assert script.count("sessionStorage.setItem(key") == 1
