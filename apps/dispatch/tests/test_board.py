from datetime import UTC, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.dispatch import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

# Anchored to today rather than pinned to a calendar date. The board hides the "Today" link
# when you are already viewing today, so a hardcoded DAY breaks the tests that assert on that
# link on the single day it happens to equal today — which is exactly what happened when this
# was pinned to 2026-08-26 and that date arrived. Staying a fixed distance from today keeps
# every test's relationship to "today" the same no matter when the suite runs.
DAY = timezone.localdate() + timedelta(days=30)


def _trip(**kwargs):
    """A booked trip on DAY unless overridden."""
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kwargs.setdefault("pickup_date", DAY)
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(lead=lead, **kwargs)


def test_board_shows_only_booked_trips_for_the_requested_day(logged_in_client):
    today = _trip()
    _trip(pickup_date=DAY + timedelta(days=1))
    _trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.status_code == 200
    assert [t.pk for t in resp.context["trips"]] == [today.pk]


def test_trips_are_ordered_by_pickup_time(logged_in_client):
    late = _trip(pickup_time=time(16, 30))
    early = _trip(pickup_time=time(6, 15))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert [t.pk for t in resp.context["trips"]] == [early.pk, late.pk]


def test_a_trip_with_no_pickup_time_sorts_first(logged_in_client):
    """NULLs sort first on MySQL and last on Postgres, so the board would read differently
    in dev and prod unless the ordering says which it wants. An untimed booked trip is an
    exception the dispatcher has to resolve — it goes on top."""
    timed = _trip(pickup_time=time(6, 15))
    untimed = _trip(pickup_time=None)
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert [t.pk for t in resp.context["trips"]] == [untimed.pk, timed.pk]


def test_the_grid_columns_are_labelled_for_what_they_hold(logged_in_client):
    """The pill column is coverage state; the provider column names whoever covers it."""
    _trip()
    body = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()}).content
    assert b">Coverage<" in body
    assert b">Covered by<" in body
    assert b">Affiliate<" not in body
    assert b">Status<" not in body


def test_the_trip_total_renders_as_money(logged_in_client):
    _trip(rate=Decimal("1200.00"), hours=1)
    body = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()}).content
    assert b"$1,200.00" in body


def test_coverage_reflects_the_active_assignment(logged_in_client):
    uncovered = _trip(pickup_time=time(6, 0))
    offered = _trip(pickup_time=time(9, 0))
    confirmed = _trip(pickup_time=time(14, 0))
    services.send_offer(offered, VendorFactory(), payout=Decimal("120.00"))
    services.assign_direct(confirmed, VendorFactory(name="Capital"), payout=Decimal("200.00"))

    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    coverage = {t.pk: t.coverage for t in resp.context["trips"]}
    assert coverage == {
        uncovered.pk: "uncovered",
        offered.pk: "offered",
        confirmed.pk: "confirmed",
    }
    assert resp.context["counts"] == {"uncovered": 1, "offered": 1, "confirmed": 1}
    assert b"Capital" in resp.content


def test_a_declined_offer_leaves_the_trip_uncovered(logged_in_client):
    trip = _trip()
    services.decline(services.send_offer(trip, VendorFactory(), payout=Decimal("120.00")))
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.context["trips"][0].coverage == "uncovered"
    assert resp.context["counts"]["uncovered"] == 1


def test_cancelled_trips_stay_off_the_board(logged_in_client):
    live = _trip(pickup_time=time(6, 0))
    _trip(pickup_time=time(9, 0), trip_status=Reservation.TripStatus.CANCELLED)
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert [t.pk for t in resp.context["trips"]] == [live.pk]
    assert resp.context["counts"]["uncovered"] == 1


def test_route_ends_come_from_the_prefetch(logged_in_client):
    _trip(stops=["IAD", "The Jefferson"])
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    rendered = resp.context["trips"][0]
    assert rendered.pickup_stop.address == "IAD"
    assert rendered.dropoff_stop.address == "The Jefferson"
    assert b"IAD" in resp.content


def test_chip_filter_narrows_the_grid(logged_in_client):
    uncovered = _trip(pickup_time=time(6, 0))
    covered = _trip(pickup_time=time(9, 0))
    services.assign_direct(covered, VendorFactory(), payout=Decimal("120.00"))
    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "f": "uncovered"}
    )
    assert [t.pk for t in resp.context["trips"]] == [uncovered.pk]
    # counts stay whole-day so the strip doesn't collapse to the filter
    assert resp.context["counts"]["confirmed"] == 1


def test_board_defaults_to_today_and_pages_by_day(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_board"))
    assert resp.context["day"] == timezone.localdate()
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    assert resp.context["prev_day"] == DAY - timedelta(days=1)
    assert resp.context["next_day"] == DAY + timedelta(days=1)


def test_a_bad_day_param_falls_back_to_today(logged_in_client):
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": "not-a-date"})
    assert resp.status_code == 200
    assert resp.context["day"] == timezone.localdate()


def test_board_requires_login(client):
    resp = client.get(reverse("dispatch_board"))
    assert resp.status_code == 302
    assert "/portal/login/" in resp["Location"]


def test_today_link_preserves_the_active_filter(logged_in_client):
    """Clicking Today while a strip filter is active should not silently clear it."""
    _trip(pickup_time=time(6, 0))
    resp = logged_in_client.get(
        reverse("dispatch_board"), {"day": DAY.isoformat(), "f": "uncovered"}
    )
    today_str = timezone.localdate().isoformat()
    assert f'href="?day={today_str}&f=uncovered"'.encode() in resp.content


def test_board_query_count_does_not_grow_with_trips(
    logged_in_client, django_assert_max_num_queries
):
    for hour in range(8, 20):  # 12 trips, each with stops and a vendor
        trip = _trip(pickup_time=time(hour, 0))
        services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
    with django_assert_max_num_queries(15):
        logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})


def test_the_board_sits_inside_the_shared_page_container(logged_in_client):
    """Every other page wraps its content in the same padded, centred container. The
    board was the one page that didn't, so it rendered edge-to-edge against the sidebar."""
    body = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()}).content
    assert b'class="max-w-[1280px] mx-auto px-4 sm:px-6 py-6"' in body


# --- the routing cell shows the flight on an airport end ---


def _with_flight(reservation, *, sequence=0, number="123"):
    """Attach IAD + United + `number` to the stop at `sequence` and return it."""
    from apps.addresses.models import Airline, Airport

    stop = reservation.stops.get(sequence=sequence)
    stop.airport = Airport.objects.get(iata="IAD")  # seeded by addresses.0003
    stop.airline = Airline.objects.get(iata="UA")
    stop.flight_number = number
    stop.flight_direction = "arrival"
    stop.save()
    return stop


def test_routing_cell_shows_the_flight_on_an_airport_end(logged_in_client):
    _with_flight(_trip())
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    html = resp.content.decode()
    assert "✈ UA 123" not in html
    assert "ti-plane-arrival" in html and "UA 123" in html


def _verified_on_board(trip, *, number="123", **over):
    """`number` distinguishes the cached Flight row — every stop otherwise defaults to the
    same airport/airline/date/direction, which would collide on Flight's uniq_flight_lookup
    constraint the moment a test links more than one trip to a "verified" flight on DAY."""
    from datetime import datetime

    from apps.reservations.factories import FlightFactory

    stop = _with_flight(trip, number=number)
    kwargs = dict(
        airline=stop.airline,
        airport=stop.airport,
        flight_number=number,
        flight_date=DAY,
        direction="arrival",
        scheduled_at=datetime.combine(DAY, datetime.min.time()).replace(
            hour=21, minute=35, tzinfo=UTC
        ),
    )
    kwargs.update(over)
    stop.flight = FlightFactory(**kwargs)
    stop.save()
    return stop


def test_board_shows_a_state_icon_and_a_compact_pill(logged_in_client):
    _verified_on_board(_trip())
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    html = resp.content.decode()
    assert 'title="Flight verified"' in html
    assert "UA 123 · 5:35 PM<" in html  # compact: no tz abbreviation on the board
    # The pill's title tooltip legitimately carries the full "Arrives ... EDT" detail text —
    # only the *visible* label must drop the abbreviation, i.e. the full (non-compact) label
    # must never be what's rendered inside the pill's <span>.
    assert "5:35 PM EDT<" not in html


def test_board_tints_delayed_and_cancelled_rows(logged_in_client):
    from apps.reservations.models import Flight

    _verified_on_board(
        _trip(pickup_time=time(9, 0)),
        number="401",
        source=Flight.Source.LIVE,
        status=Flight.Status.ACTIVE,
        delay_minutes=40,
    )
    _verified_on_board(
        _trip(pickup_time=time(10, 0)),
        number="402",
        source=Flight.Source.LIVE,
        status=Flight.Status.CANCELLED,
    )
    resp = logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
    html = resp.content.decode()
    assert 'title="1 flight delayed"' in html and "bg-amber-50/40" in html
    assert 'title="Flight cancelled"' in html and "bg-rose-50/40" in html


def test_board_flight_joins_keep_the_query_bound(logged_in_client, django_assert_max_num_queries):
    for hour in range(8, 20):
        trip = _trip(pickup_time=time(hour, 0))
        services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
        _verified_on_board(trip, number=str(hour))
    with django_assert_max_num_queries(15):
        logged_in_client.get(reverse("dispatch_board"), {"day": DAY.isoformat()})
