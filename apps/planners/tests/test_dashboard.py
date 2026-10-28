"""APC-67: the planner's read-only wedding dashboard."""

import re
import uuid
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging.models import TouchPoint
from apps.planners import services
from apps.reservations.models import Reservation, Stop
from apps.tasks.models import Task, TaskConfig
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-pass-phrase-77"
NY, CHI = "America/New_York", "America/Chicago"


@pytest.fixture
def planner(client):
    contact = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    services.register(account, PASSWORD)
    client.post(reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD})
    return account


def _day(offset=30):
    return timezone.localdate() + timedelta(days=offset)


def _trip(
    lead,
    *,
    at=time(15, 0),
    day=None,
    zone=NY,
    vehicle=None,
    group=None,
    stops=None,
    service="Ceremony shuttle",
    status="",
):
    trip = Reservation.objects.create(
        lead=lead,
        vehicle=vehicle or VehicleTypeFactory(name="Executive Sprinter", capacity=14),
        service_type=ServiceTypeFactory(name=service),
        pickup_date=day or _day(),
        pickup_time=at,
        pickup_timezone=zone,
        rate=7391,
        hours=4,
        affiliate_cost=6113,
        group_key=group,
        trip_status=status,
    )
    for i, (name, address) in enumerate(
        stops or [("The Hay-Adams", "800 16th St NW"), ("Anderson House", "2118 Mass Ave NW")]
    ):
        Stop.objects.create(
            reservation=trip, sequence=i, name=name, address=address, note="gate code 4471"
        )
    return trip


def _wedding(account, *, trips=1, role=LeadContact.Role.LEAD_PLANNER, **kw):
    agent = UserFactory(first_name="Dana", last_name="Reyes", phone="+12025550148")
    lead = LeadFactory(
        status=Lead.Status.BOOKED,
        wedding_name="Whitfield & Okafor",
        assigned_agent=agent,
        notes="INTERNAL: client is price sensitive",
        intake_payload={
            "wedding_date": _day().isoformat(),
            "groups": ["guests"],
            "guest_count": 180,
            "venue_name": "Anderson House",
            "hotels": [{"name": "The Hay-Adams"}],
        },
    )
    LeadContact.objects.create(lead=lead, contact=account.contact, role=role)
    made = [_trip(lead, at=time(13 + (i % 8), 0), **kw) for i in range(trips)]
    return lead, made


def _page(client, lead):
    response = client.get(reverse("planners:wedding", args=[lead.pk]))
    assert response.status_code == 200
    return response.content.decode()


def _driver(trip, **kw):
    return AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Secret Affiliate Livery"),
        status=Assignment.Status.CONFIRMED,
        payout=4327,
        driver_name="Marcus Bell",
        driver_cell="+12025550142",
        vehicle_desc="Black Sprinter",
        vehicle_number="12",
        **kw,
    )


# --- access ---------------------------------------------------------------------------


def test_renders_for_a_planner_with_a_role(client, planner):
    lead, _ = _wedding(planner)
    html = _page(client, lead)
    assert "Whitfield &amp; Okafor" in html


def test_404_for_a_wedding_they_hold_no_role_on(client, planner):
    other = LeadFactory(status=Lead.Status.BOOKED)
    _trip(other)
    assert client.get(reverse("planners:wedding", args=[other.pk])).status_code == 404


def test_signed_out_goes_to_sign_in(client):
    lead = LeadFactory()
    response = client.get(reverse("planners:wedding", args=[lead.pk]))
    assert response.status_code == 302
    assert response.url.startswith(reverse("planners:login"))


# --- header, places, schedule ---------------------------------------------------------


def test_header_names_the_day_the_guests_and_the_coordinator(client, planner):
    lead, _ = _wedding(planner)
    html = _page(client, lead)
    assert "180" in html and "guests" in html
    assert "Dana Reyes" in html
    assert "tel:+12025550148" in html
    assert f"{_day():%B} {_day().day}, {_day().year}" in html


def test_venues_and_hotels_come_from_the_trip_stops(client, planner):
    lead, _ = _wedding(planner)
    html = _page(client, lead)
    assert "The Hay-Adams" in html and "800 16th St NW" in html
    assert "Anderson House" in html
    assert html.index("Hotels") < html.index("800 16th St NW")


def test_schedule_groups_a_linked_set_into_one_run_with_its_manifest(client, planner):
    key = uuid.uuid4()
    lead, trips = _wedding(planner, trips=3, group=key)
    _trip(lead, service="Rehearsal dinner", day=_day(29))
    html = _page(client, lead)
    assert html.count('data-run="') == 2
    assert "3 vehicles" in html and "42 seats" in html
    assert "Executive Sprinter" in html
    assert html.index("Rehearsal dinner") < html.index("Ceremony shuttle")  # day order


def test_cancelled_trips_are_left_off(client, planner):
    lead, _ = _wedding(planner)
    _trip(lead, service="Cancelled run", status=Reservation.TripStatus.CANCELLED)
    assert "Cancelled run" not in _page(client, lead)


def test_every_leg_shows_its_own_timezone_abbreviation(client, planner):
    lead, _ = _wedding(planner)
    _trip(
        lead,
        zone=CHI,
        at=time(7, 0),
        service="Family travel",
        stops=[("Private residence", "Brentwood, TN"), ("BNA", "Nashville airport")],
    )
    html = _page(client, lead)
    day = _day()
    ny = datetime.combine(day, time(13, 0), tzinfo=ZoneInfo(NY)).tzname()
    chi = datetime.combine(day, time(7, 0), tzinfo=ZoneInfo(CHI)).tzname()
    assert re.search(rf"1:00 PM.*?{ny}", html, re.S)
    assert re.search(rf"7:00 AM.*?{chi}", html, re.S)


def test_before_any_trips_exist_the_day_is_being_built(client, planner):
    lead, _ = _wedding(planner, trips=0)
    html = _page(client, lead)
    assert "building your day" in html


# --- drivers --------------------------------------------------------------------------


def test_driver_details_are_hidden_until_the_trip_is_dispatched(client, planner):
    lead, (trip,) = _wedding(planner)
    _driver(trip)
    assert "Marcus Bell" not in _page(client, lead)

    trip.trip_status = Reservation.TripStatus.DISPATCHED
    trip.save(update_fields=["trip_status"])
    html = _page(client, lead)
    assert "Marcus Bell" in html and "tel:+12025550142" in html and "Black Sprinter" in html


def test_driver_release_alone_does_not_show_the_driver(client, planner):
    lead, (trip,) = _wedding(planner)
    _driver(trip)
    TouchPoint.objects.create(
        lead=lead,
        reservation=trip,
        kind=TouchPoint.Kind.DRIVER_RELEASED,
        status=TouchPoint.Status.SENT,
        sent_at=timezone.now(),
    )
    assert "Marcus Bell" not in _page(client, lead)


def test_a_dispatched_non_la_trip_and_later_statuses_show_the_driver(client, planner):
    lead, (trip,) = _wedding(planner)
    _driver(trip)
    for status in (Reservation.TripStatus.DISPATCHED_NON_LA, Reservation.TripStatus.ON_THE_WAY):
        Reservation.objects.filter(pk=trip.pk).update(trip_status=status)
        assert "Marcus Bell" in _page(client, lead)


def test_the_dispatch_number_is_on_the_page_in_every_state(client, planner):
    lead, _ = _wedding(planner)
    assert "tel:+12024242600" in _page(client, lead)


# --- outstanding items ----------------------------------------------------------------


def _task(lead, kind, status=Task.Status.OPEN, due=None):
    return Task.objects.create(
        lead=lead,
        kind=kind,
        department="customer_service",
        status=status,
        opens_at=timezone.now(),
        due_at=due or timezone.now() + timedelta(days=3),
    )


def test_outstanding_items_are_the_open_planner_facing_tasks_label_and_due_only(client, planner):
    lead, (trip,) = _wedding(planner)
    due = datetime(2026, 11, 7, 4, 0, tzinfo=ZoneInfo("UTC"))  # Nov 6 local (NY)
    _task(lead, "day_of_contact", due=due)
    _task(lead, "final_itinerary")
    _task(lead, "wedding_names", status=Task.Status.DONE)
    _task(lead, "deposit_received")
    html = _page(client, lead)
    assert "Waiting on you" in html
    assert "Day-of contact" in html and "Final itinerary" in html
    assert "Due Fri, Nov 6" in html
    assert "Wedding names" not in html
    assert "Deposit" not in html


def test_nothing_waiting_collapses_to_one_line(client, planner):
    lead, _ = _wedding(planner)
    assert "Nothing is waiting on you" in _page(client, lead)


# --- what must never be on this page --------------------------------------------------


def test_no_money_affiliate_cost_or_internal_notes_in_the_page(client, planner):
    lead, (trip,) = _wedding(planner)
    _driver(trip)
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.DISPATCHED)
    # The static files' cache-buster (`?v=<mtime>`) is digits too; it must not read as a leak.
    html = re.sub(r"\?v=\d+", "", _page(client, lead))
    assert not re.search(r"\$\s?\d", html)
    for leak in (
        "7391",
        "6113",
        "4327",
        "29564",
        "29,564",
        "Secret Affiliate",
        "INTERNAL",
        "price sensitive",
        "gate code",
        "payout",
        "affiliate",
    ):
        assert leak.lower() not in html.lower(), leak


# --- archive ----------------------------------------------------------------------------


def test_an_archived_wedding_renders_with_the_marker(client, planner):
    lead, (trip,) = _wedding(planner, day=_day(-10))
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.DONE)
    html = _page(client, lead)
    assert "Archived" in html
    assert "Whitfield &amp; Okafor" in html


# --- cost ---------------------------------------------------------------------------------


def _queries(client, lead) -> int:
    with CaptureQueriesContext(connection) as ctx:
        _page(client, lead)
    return len(ctx.captured_queries)


def test_query_count_does_not_grow_with_the_number_of_trips(client, planner):
    small, _ = _wedding(planner, trips=2)
    big, trips = _wedding(planner, trips=12)
    for t in trips:
        _driver(t)
        _task(big, "day_of_contact")
    Reservation.objects.filter(lead=big).update(trip_status=Reservation.TripStatus.DISPATCHED)
    for t in small.reservations.all():
        _driver(t)
    TaskConfig.load()  # the singleton's first-ever read is an insert; not the page's cost
    assert _queries(client, big) == _queries(client, small)
    assert _queries(client, big) <= 12


# --- which orders a planner sees, and when the schedule shows (Moe, 2026-10-25) ----------


def test_an_engaged_order_is_still_being_built_until_it_is_booked(client, planner):
    lead, _ = _wedding(planner, status="")
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.ENGAGED)

    html = _page(client, lead)

    assert "building your day" in html
    assert "Ceremony shuttle" not in html


def test_a_booked_order_shows_its_schedule(client, planner):
    lead, _ = _wedding(planner)

    html = _page(client, lead)

    assert "Ceremony shuttle" in html
    assert "building your day" not in html


def test_a_quote_stays_visible_until_its_first_trip_date(client, planner):
    lead, _ = _wedding(planner, day=timezone.localdate())
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.QUOTED)

    assert lead.pk in set(services.weddings_for(planner).values_list("pk", flat=True))
    assert client.get(reverse("planners:wedding", args=[lead.pk])).status_code == 200


def test_a_quote_whose_first_trip_date_has_passed_drops_off(client, planner):
    lead, _ = _wedding(planner, day=timezone.localdate() - timedelta(days=1))
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.QUOTED)

    assert lead.pk not in set(services.weddings_for(planner).values_list("pk", flat=True))
    assert client.get(reverse("planners:wedding", args=[lead.pk])).status_code == 404
    assert "Whitfield &amp; Okafor" not in client.get(reverse("planners:weddings")).content.decode()


def test_a_booked_wedding_in_the_past_stays_as_an_archive(client, planner):
    lead, _ = _wedding(planner, day=timezone.localdate() - timedelta(days=40))

    assert client.get(reverse("planners:wedding", args=[lead.pk])).status_code == 200


def test_the_first_trip_date_is_read_in_that_trips_own_timezone():
    from apps.planners.services import first_trip_passed

    now = datetime(2026, 10, 25, 3, 30, tzinfo=ZoneInfo("UTC"))  # Oct 24, 11:30 PM in New York
    lead = LeadFactory(status=Lead.Status.QUOTED)
    _trip(lead, day=datetime(2026, 10, 24).date(), zone=NY)

    assert first_trip_passed(lead, now=now) is False
    assert first_trip_passed(lead, now=now + timedelta(hours=1)) is True


def test_the_staff_access_card_counts_what_the_planner_sees(client, planner):
    from apps.planners.selectors import access_for

    _wedding(planner)
    stale, _ = _wedding(planner, day=timezone.localdate() - timedelta(days=3))
    Lead.objects.filter(pk=stale.pk).update(status=Lead.Status.NEW)

    assert access_for(planner.contact)["weddings"] == 1
