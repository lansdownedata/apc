"""The drawer's trip status control, its dialogs' endpoints, and the board's notices
(trip-status cleanup, Task 7)."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db
TS = Reservation.TripStatus

ELEVEN = [
    "pending",
    "unassigned",
    "assigned",
    "dispatched",
    "on_the_way",
    "arrived",
    "customer_in_car",
    "done",
    "cancelled",
    "late_cancel",
    "no_show",
]
DRAWER = ["dispatched", "on_the_way", "arrived", "customer_in_car", "done"]


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("pickup_date", date(2026, 11, 20))
    kwargs.setdefault("pickup_time", time(6, 15))
    kwargs.setdefault("trip_status", TS.UNASSIGNED)
    return ReservationFactory(**kwargs)


def _confirmed(trip, **kwargs):
    kwargs.setdefault("vendor", VendorFactory())
    kwargs.setdefault("status", Assignment.Status.CONFIRMED)
    return AssignmentFactory(reservation=trip, **kwargs)


def _panel(client, trip):
    return client.get(reverse("dispatch_assign_panel", args=[trip.pk]))


def _status_options(body: str) -> list[tuple[str, bool, bool, str]]:
    """(value, disabled, selected, label) for each option of the drawer's status select."""
    select = re.search(r'<select name="status" id="trip-status".*?</select>', body, re.DOTALL)
    assert select, "no trip status select in the drawer"
    assert "data-tom" in select.group(0)
    return [
        (value, " disabled" in attrs, " selected" in attrs, label.strip())
        for value, attrs, label in re.findall(
            r'<option value="([^"]*)"([^>]*)>([^<]*)</option>', select.group(0)
        )
    ]


def _text(body: str) -> str:
    """The page's text with tags dropped and whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).replace(" ,", ",")


# --- the select ------------------------------------------------------------------------


def test_drawer_lists_all_eleven_statuses_with_coverage_owned_ones_disabled(logged_in_client):
    trip = _trip(trip_status=TS.ON_THE_WAY)
    _confirmed(trip)

    options = _status_options(_panel(logged_in_client, trip).content.decode())

    assert [o[0] for o in options] == ELEVEN
    disabled = {o[0] for o in options if o[1]}
    assert {"pending", "unassigned", "assigned"} <= disabled
    assert not disabled & set(DRAWER)
    assert [o[0] for o in options if o[2]] == ["on_the_way"]
    labels = dict((o[0], o[3]) for o in options)
    assert labels["dispatched"] == "Dispatched"
    assert labels["customer_in_car"] == "Customer In Car"


def test_drawer_statuses_wait_for_confirmed_coverage(logged_in_client):
    trip = _trip()

    options = _status_options(_panel(logged_in_client, trip).content.decode())

    disabled = {o[0] for o in options if o[1]}
    assert set(DRAWER) <= disabled
    # A no-show needs no coverage: a passenger can fail to appear on an uncovered trip.
    assert "late_cancel" not in disabled and "no_show" not in disabled
    assert [o[0] for o in options if o[2]] == ["unassigned"]


def test_cancelled_needs_payments_access(logged_in_client, client):
    trip = _trip()

    agent = _status_options(_panel(logged_in_client, trip).content.decode())
    assert ("cancelled", True) in [(o[0], o[1]) for o in agent]

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = _panel(client, trip)
    owner = _status_options(resp.content.decode())
    assert ("cancelled", False) in [(o[0], o[1]) for o in owner]
    cancel = resp.context["status_control"]["cancel"]
    assert cancel["kind"] == "refund"
    assert cancel["url"] == reverse("order_cancel_refund", args=[trip.lead_id])
    assert cancel["preselect"] == trip.pk
    assert [row["pk"] for row in cancel["trips"]] == [trip.pk]


def test_a_cancelled_trip_cannot_become_a_late_cancel(logged_in_client):
    trip = _trip(trip_status=TS.CANCELLED)

    options = _status_options(_panel(logged_in_client, trip).content.decode())

    disabled = {o[0] for o in options if o[1]}
    assert {"late_cancel", "no_show", "cancelled"} <= disabled


def test_status_control_carries_the_endpoints(logged_in_client):
    trip = _trip()

    control = _panel(logged_in_client, trip).context["status_control"]

    assert control["current"] == "unassigned"
    assert control["setUrl"] == reverse("dispatch_set_status", args=[trip.pk])
    assert control["cancelStatusUrl"] == reverse("dispatch_cancel_status", args=[trip.pk])


# --- set_status --------------------------------------------------------------------------


@pytest.mark.parametrize("status", DRAWER)
def test_set_status_accepts_each_drawer_status(logged_in_client, status):
    trip = _trip(trip_status=TS.DISPATCHED)
    _confirmed(trip)

    resp = logged_in_client.post(reverse("dispatch_set_status", args=[trip.pk]), {"status": status})

    assert resp.status_code == 200, resp.content
    trip.refresh_from_db()
    assert trip.trip_status == status


@pytest.mark.parametrize(
    "status", ["cancelled", "unassigned", "pending", "assigned", "late_cancel", "no_show"]
)
def test_set_status_refuses_cancel_and_coverage_owned_statuses(logged_in_client, status):
    trip = _trip(trip_status=TS.DISPATCHED)
    _confirmed(trip)

    resp = logged_in_client.post(reverse("dispatch_set_status", args=[trip.pk]), {"status": status})

    assert resp.status_code == 400
    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED


# --- badges -------------------------------------------------------------------------------


def _badges(client, trip) -> dict[str, str]:
    """The drawer's status badges, by key, with their text."""
    body = _panel(client, trip).content.decode()
    return {
        key: _text(text).strip()
        for key, text in re.findall(r'data-badge="([^"]+)"[^>]*>(.*?)</span>', body, re.DOTALL)
    }


def test_drawer_badges_an_affiliate_and_their_driver(logged_in_client):
    trip = _trip()
    assignment = services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))

    assert _badges(logged_in_client, trip) == {"affiliate": "Assigned to affiliate"}

    assignment.driver_info_at = timezone.now()
    assignment.save(update_fields=["driver_info_at"])
    assert _badges(logged_in_client, trip) == {
        "affiliate": "Assigned to affiliate",
        "driver": "Driver assigned",
    }


def test_drawer_badges_our_own_driver(logged_in_client):
    trip = _trip()
    services.assign_in_house(trip, DriverFactory())

    assert _badges(logged_in_client, trip) == {"driver": "Driver assigned"}


def test_uncovered_drawer_has_neither_badge(logged_in_client):
    assert _badges(logged_in_client, _trip()) == {}


# --- needs farm-out ------------------------------------------------------------------------


def test_needs_farm_out_toggles(logged_in_client):
    trip = _trip()
    url = reverse("dispatch_needs_farm_out", args=[trip.pk])

    assert logged_in_client.post(url, {"on": "1"}).json() == {"ok": True}
    trip.refresh_from_db()
    assert trip.needs_farm_out is True

    assert logged_in_client.post(url, {"on": "0"}).json() == {"ok": True}
    trip.refresh_from_db()
    assert trip.needs_farm_out is False


def test_needs_farm_out_is_post_only(logged_in_client):
    trip = _trip()
    assert (
        logged_in_client.get(reverse("dispatch_needs_farm_out", args=[trip.pk])).status_code == 405
    )


def test_drawer_shows_the_farm_out_toggle(logged_in_client):
    trip = _trip(needs_farm_out=True)

    body = _panel(logged_in_client, trip).content.decode()

    assert reverse("dispatch_needs_farm_out", args=[trip.pk]) in body
    assert "Needs farm-out" in body
    assert 'aria-pressed="true"' in body


# --- circling -------------------------------------------------------------------------------


def test_drawer_shows_the_circling_banner_with_dismiss(logged_in_client):
    trip = _trip(trip_status=TS.ARRIVED, circling_at=timezone.now() - timedelta(minutes=4))
    _confirmed(trip)

    body = _panel(logged_in_client, trip).content.decode()

    assert "data-circling-banner" in body
    assert "Circling · 4 min" in _text(body)
    assert reverse("dispatch_circling", args=[trip.pk]) in body
    assert "Dismiss" in body


def test_drawer_offers_mark_circling_on_an_arrived_trip(logged_in_client):
    trip = _trip(trip_status=TS.ARRIVED)
    _confirmed(trip)

    body = _panel(logged_in_client, trip).content.decode()

    assert "data-circling-banner" not in body
    assert "Mark circling" in body


def test_no_circling_control_before_the_driver_is_moving(logged_in_client):
    body = _panel(logged_in_client, _trip()).content.decode()
    assert "Mark circling" not in body and "data-circling-banner" not in body


# --- the board ---------------------------------------------------------------------------------


def _row(client, trip) -> str:
    body = client.get(reverse("dispatch_board"), {"day": trip.pickup_date.isoformat()})
    row = re.search(rf'<tr data-trip="{trip.pk}".*?</tr>', body.content.decode(), re.DOTALL)
    return _text(row.group(0))


def test_board_row_shows_circling_minutes(logged_in_client):
    day = timezone.localdate() + timedelta(days=1)
    trip = _trip(
        pickup_date=day,
        trip_status=TS.ARRIVED,
        circling_at=timezone.now() - timedelta(minutes=4, seconds=10),
    )
    _confirmed(trip)

    assert "Circling · 4 min" in _row(logged_in_client, trip)


def test_board_row_shows_needs_farm_out(logged_in_client):
    day = timezone.localdate() + timedelta(days=1)
    flagged = _trip(pickup_date=day, needs_farm_out=True)
    plain = _trip(pickup_date=day)

    assert "Needs farm-out" in _row(logged_in_client, flagged)
    assert "Needs farm-out" not in _row(logged_in_client, plain)
    assert "Circling" not in _row(logged_in_client, plain)


# --- sent back --------------------------------------------------------------------------------


def test_sent_back_is_offered_on_confirmed_affiliate_coverage(logged_in_client):
    trip = _trip()
    assignment = services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))

    body = _panel(logged_in_client, trip).content.decode()

    assert reverse("dispatch_send_back", args=[assignment.pk]) in body
    assert "Sent back" in body


def test_sent_back_is_not_offered_in_house_or_on_an_offer(logged_in_client):
    in_house = _trip()
    services.assign_in_house(in_house, DriverFactory())
    offered = _trip()
    _confirmed(offered, status=Assignment.Status.OFFERED)

    for trip in (in_house, offered):
        assert "/send-back/" not in _panel(logged_in_client, trip).content.decode()


def test_sent_back_is_not_offered_on_gnet(logged_in_client):
    """A GNet affiliate gives a trip back over GNet (its CANCEL becomes Sent back); a hand
    mark would leave the booking live on the gateway."""
    trip = _trip()
    _confirmed(trip, channel=Assignment.Channel.GNET)

    assert "/send-back/" not in _panel(logged_in_client, trip).content.decode()


# --- the cancel line -------------------------------------------------------------------------


def _cancelled(status=TS.CANCELLED, covid=True, **kwargs):
    who = UserFactory(first_name="Dana", last_name="Reyes")
    return _trip(
        trip_status=status,
        cancel_reason="Flight grounded",
        cancel_covid=covid,
        cancelled_at=datetime(2026, 11, 3, 14, 15, tzinfo=UTC),
        cancelled_by=who,
        **kwargs,
    )


LINE = "Cancelled · COVID-19 — Flight grounded · Dana Reyes, Nov 3, 9:15 AM EST"


def test_cancel_line_in_the_drawer(logged_in_client):
    trip = _cancelled()
    assert LINE in _text(_panel(logged_in_client, trip).content.decode())


def test_cancel_line_on_the_order_page(logged_in_client):
    trip = _cancelled()
    body = logged_in_client.get(reverse("order_detail", args=[trip.lead_id])).content.decode()
    assert _text(body).count(LINE) == 1


def test_cancel_line_on_the_quote_workspace(logged_in_client):
    trip = _cancelled()
    body = logged_in_client.get(reverse("lead_detail", args=[trip.lead_id])).content.decode()
    assert _text(body).count(LINE) == 1


def test_cancel_line_names_covid_only_when_ticked(logged_in_client):
    trip = _cancelled(covid=False)
    body = _text(_panel(logged_in_client, trip).content.decode())
    assert "Cancelled — Flight grounded · Dana Reyes, Nov 3, 9:15 AM EST" in body
    assert "COVID-19" not in body


def test_cancel_line_for_a_no_show_in_the_trip_zone(logged_in_client):
    trip = _cancelled(status=TS.NO_SHOW, covid=False, pickup_timezone="America/Los_Angeles")
    body = _text(_panel(logged_in_client, trip).content.decode())
    assert "No Show — Flight grounded · Dana Reyes, Nov 3, 6:15 AM PST" in body


def test_no_cancel_line_on_a_live_trip(logged_in_client):
    assert "data-cancel-line" not in _panel(logged_in_client, _trip()).content.decode()
