import re
from datetime import UTC, date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.dispatch import selectors, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations.factories import HourlyReservationFactory, ReservationFactory
from apps.vendors.factories import VendorFactory, VendorInsuranceFactory

pytestmark = pytest.mark.django_db


def _trip(**kwargs):
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(lead=lead, **kwargs)


def test_vendors_are_ranked_by_how_often_they_are_used():
    trip = _trip()
    heavy, light, unused = VendorFactory(), VendorFactory(), VendorFactory()
    for _ in range(3):
        services.decline(services.send_offer(_trip(), heavy, payout=Decimal("10.00")))
    services.decline(services.send_offer(_trip(), light, payout=Decimal("10.00")))

    ranked = selectors.vendor_options(trip)
    assert [o["vendor"].pk for o in ranked[:2]] == [heavy.pk, light.pk]
    assert ranked[0]["used"] == 3
    assert unused.pk in [o["vendor"].pk for o in ranked]


def test_ranking_is_capped_at_the_limit():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    assert len(selectors.vendor_options(trip, limit=8)) == 8


def test_vendor_options_query_count_is_flat_regardless_of_vendor_count(
    django_assert_max_num_queries,
):
    """Guards against reintroducing an N+1, e.g. a per-vendor insurance lookup.

    The bound must hold at two very different vendor counts to prove the query count is
    flat, not just under some ceiling that a mild N+1 would still clear.
    """
    trip = _trip()
    for _ in range(3):
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)

    for _ in range(27):  # 30 active vendors total
        VendorFactory()
    with django_assert_max_num_queries(2):
        selectors.vendor_options(trip)


def test_search_looks_past_the_top_slice():
    trip = _trip()
    for _ in range(12):
        VendorFactory()
    needle = VendorFactory(name="Zebra Executive")
    assert [o["vendor"].pk for o in selectors.vendor_options(trip, search="zebra")] == [needle.pk]


def test_options_carry_vehicle_fit():
    suv = VehicleTypeFactory(name="SUV")
    trip = _trip(vehicle=suv)
    fitting = VendorFactory()
    fitting.vehicle_types.add(suv)
    VendorFactory()
    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[fitting.pk]["fits_vehicle"] is True
    assert all(o["fits_vehicle"] is False for pk, o in by_pk.items() if pk != fitting.pk)


def test_options_carry_the_insurance_state():
    """Lapsed coverage is the one fit signal with legal consequences for a broker, and the
    drawer is where the assignment decision actually gets made."""
    trip = _trip()
    lapsed = VendorFactory(name="Lapsed Ltd")
    VendorInsuranceFactory(vendor=lapsed, expiry_date=timezone.localdate() - timedelta(days=3))
    insured = VendorFactory(name="Insured Co")
    VendorInsuranceFactory(vendor=insured, expiry_date=timezone.localdate() + timedelta(days=200))
    bare = VendorFactory(name="Bare Inc")

    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[lapsed.pk]["insurance"]["status"] == "expired"
    assert by_pk[insured.pk]["insurance"]["status"] == "valid"
    assert by_pk[bare.pk]["insurance"]["status"] == "none"


def test_panel_shows_the_insurance_state(logged_in_client):
    trip = _trip()
    lapsed = VendorFactory(name="Lapsed Ltd")
    VendorInsuranceFactory(vendor=lapsed, expiry_date=timezone.localdate() - timedelta(days=3))
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content
    assert b"Lapsed 3 days ago" in body


def test_panel_renders_money_as_money(logged_in_client):
    trip = _trip(rate=Decimal("1200.00"), hours=1)
    services.assign_direct(trip, VendorFactory(), payout=Decimal("1000.00"))
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content
    assert b"$1,200.00" in body  # customer total
    assert b"$1,000.00" in body  # payout
    assert b"$200.00" in body  # margin


def test_panel_renders_the_trip_and_vendors(logged_in_client):
    trip = _trip()
    VendorFactory(name="Capital Chauffeurs")
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 200
    assert b"Capital Chauffeurs" in resp.content
    assert resp.context["trip"].pk == trip.pk
    assert resp.context["assignment"] is None


def test_panel_shows_the_active_assignment_when_there_is_one(logged_in_client):
    trip = _trip()
    services.assign_direct(trip, VendorFactory(name="Chesapeake"), payout=Decimal("200.00"))
    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.context["assignment"].vendor.name == "Chesapeake"
    assert b"Chesapeake" in resp.content


def test_panel_requires_login(client):
    trip = _trip()
    resp = client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
    assert resp.status_code == 302


# --- GNet channel hint ---


def test_options_carry_the_gnet_flag():
    trip = _trip()
    grid = VendorFactory(name="Grid Co", gnet_grid_id="gnet-1")
    manual = VendorFactory(name="Manual Co")
    by_pk = {o["vendor"].pk: o for o in selectors.vendor_options(trip)}
    assert by_pk[grid.pk]["is_gnet"] is True
    assert by_pk[manual.pk]["is_gnet"] is False


def test_panel_shows_the_gnet_badge_for_a_gnet_capable_vendor_only(logged_in_client):
    trip = _trip()
    VendorFactory(name="Grid Co", gnet_grid_id="gnet-1")
    VendorFactory(name="Manual Co")
    body = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    grid_label = next(label for label in labels if "Grid Co" in label)
    manual_label = next(label for label in labels if "Manual Co" in label)
    assert "GNET" in grid_label
    assert "GNET" not in manual_label


# --- staff-marking buttons are for non-GNet vendors only ---


def _panel(logged_in_client, trip) -> str:
    return logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()


def test_panel_hides_confirm_and_declined_for_a_gnet_offer(logged_in_client):
    """A GNet offer resolves from the affiliate's callback. Staff-marking it declined
    would strand a real booking (see test_mutations), so the buttons aren't rendered —
    with a line of copy so their absence isn't a mystery."""
    trip = _trip()
    AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Grid Co", gnet_grid_id="gnet-1"),
        channel=Assignment.Channel.GNET,
        status=Assignment.Status.OFFERED,
    )
    body = _panel(logged_in_client, trip)

    assert ">Confirm<" not in body
    assert ">Declined<" not in body
    assert ">Withdraw<" in body
    assert "affiliate" in body.lower()


def test_panel_keeps_confirm_and_declined_for_a_manual_offer(logged_in_client):
    trip = _trip()
    services.send_offer(trip, VendorFactory(name="Manual Co"), payout=Decimal("100.00"))
    body = _panel(logged_in_client, trip)

    assert ">Confirm<" in body
    assert ">Declined<" in body
    assert ">Withdraw<" in body


# --- a GNet affiliate needs no email address ---


def test_the_send_button_is_not_gated_on_email_for_a_gnet_vendor(logged_in_client):
    """The GNet channel doesn't use email at all, so gating Send offer on a vendor
    email blocked exactly the affiliates this channel exists for."""
    trip = _trip()
    VendorFactory(name="Grid Co", gnet_grid_id="gnet-1", email="")
    body = _panel(logged_in_client, trip)

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    grid_label = next(label for label in labels if "Grid Co" in label)
    assert 'data-gnet="1"' in grid_label
    # Both the button's :disabled expression and the hint's x-show must let it through.
    assert body.count("selectedGnet") >= 3


def test_a_non_gnet_vendor_still_carries_an_empty_gnet_flag(logged_in_client):
    trip = _trip()
    VendorFactory(name="Manual Co", gnet_grid_id="", email="")
    body = _panel(logged_in_client, trip)

    labels = re.findall(r"<label\b.*?</label>", body, re.DOTALL)
    manual_label = next(label for label in labels if "Manual Co" in label)
    assert 'data-gnet=""' in manual_label


# --- preview mode must be visible in the drawer, not just in Django admin ---


def test_panel_flags_an_offer_that_preview_mode_never_sent(logged_in_client, settings):
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    trip = _trip()
    services.send_offer(trip, VendorFactory(gnet_grid_id="gnet-1"), payout=Decimal("100.00"))

    body = _panel(logged_in_client, trip)

    assert "preview" in body.lower()


def test_panel_does_not_flag_preview_for_a_manual_offer(logged_in_client, settings):
    settings.GNET_ACTIVE = False
    trip = _trip()
    services.send_offer(trip, VendorFactory(email="ops@x.example"), payout=Decimal("100.00"))

    body = _panel(logged_in_client, trip)

    assert "preview" not in body.lower()


# --- the drawer is a trip sheet, not a three-line recap ---


def _stop(trip, sequence: int):
    return trip.stops.get(sequence=sequence)


def test_panel_lists_every_stop_in_sequence(logged_in_client):
    trip = _trip(stops=["Dulles Airport", "The Jefferson Hotel", "Union Station"])
    body = _panel(logged_in_client, trip)
    assert body.index("Dulles Airport") < body.index("The Jefferson Hotel")
    assert body.index("The Jefferson Hotel") < body.index("Union Station")


def test_panel_shows_a_stops_venue_time_and_note(logged_in_client):
    trip = _trip(stops=["A", "B", "C"])
    middle = _stop(trip, 1)
    middle.name = "Lincoln Memorial"
    middle.scheduled_time = time(7, 45)
    middle.note = "20-min photo stop"
    middle.save()

    body = _panel(logged_in_client, trip)

    assert "Lincoln Memorial" in body
    assert "7:45 AM" in body
    assert "20-min photo stop" in body


def test_panel_shows_the_passenger_contact(logged_in_client):
    contact = ContactFactory(name="Ada Kavanagh", phone="+1 202 555 0143", email="ada@example.com")
    trip = _trip(lead=LeadFactory(status=Lead.Status.BOOKED, contact=contact), passengers=4)
    body = _panel(logged_in_client, trip)
    assert "Ada Kavanagh" in body
    assert "+1 202 555 0143" in body
    assert "ada@example.com" in body
    assert "4 pax" in body


def test_panel_shows_the_schedule_for_a_transfer(logged_in_client):
    trip = _trip(
        pickup_date=date(2026, 8, 26),
        pickup_time=time(6, 15),
        dropoff_date=date(2026, 8, 26),
        dropoff_time=time(7, 30),
    )
    body = _panel(logged_in_client, trip)
    assert "Aug 26, 2026" in body
    assert "6:15 AM" in body
    assert "7:30 AM" in body


def test_panel_shows_the_billed_hours_for_an_hourly_trip(logged_in_client):
    trip = HourlyReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 8, 26),
        pickup_time=time(18, 0),
        hours=5,
        min_hours=4,
    )
    body = _panel(logged_in_client, trip)
    assert "5 hrs" in body


def test_panel_shows_service_trip_type_and_vehicle(logged_in_client):
    trip = _trip(service="Wedding shuttle", vehicle=VehicleTypeFactory(name="Sprinter"))
    body = _panel(logged_in_client, trip)
    assert "Wedding shuttle" in body
    assert "Transfer" in body
    assert "Sprinter" in body


def test_panel_links_to_the_quote_workspace(logged_in_client):
    trip = _trip()
    body = _panel(logged_in_client, trip)
    assert reverse("lead_detail", args=[trip.lead.pk]) in body
    assert trip.lead.quote_no in body


def test_panel_route_comes_from_one_stops_query(logged_in_client, django_assert_max_num_queries):
    """The whole route renders from a single prefetch. `Reservation.pickup`/`dropoff` cost a
    query each, and a per-stop lookup would scale with the route — either pushes this over."""
    trip = _trip(stops=[f"Stop {i}" for i in range(8)])
    # Budget 10, not 9: selectors.in_house_options adds one query (the active-drivers read)
    # even when there are no drivers to show.
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))


# --- the drawer's route rail shows the flight, with a Verify button ---


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


def test_panel_shows_the_flight_with_a_verify_button(logged_in_client, settings):
    settings.AVIATIONSTACK_API_KEY = "k"
    trip = _trip()
    _with_flight(trip)
    body = _panel(logged_in_client, trip)
    assert "flightVerifyComingSoon" not in body
    assert "flightStatus({" in body
    assert "&quot;direction&quot;: &quot;arrival&quot;" in body  # payload for the endpoint
    assert "&quot;date&quot;: &quot;2026-08-26&quot;" in body
    assert "initial: null" in body and "enabled: true" in body
    assert "UA 123" in body


def test_panel_verified_flight_carries_its_pill(logged_in_client):
    from datetime import datetime

    from apps.reservations.factories import FlightFactory

    trip = _trip()
    stop = _with_flight(trip)
    stop.flight = FlightFactory(
        airline=stop.airline,
        airport=stop.airport,
        flight_number="123",
        flight_date=date(2026, 8, 26),
        direction="arrival",
        scheduled_at=datetime(2026, 8, 26, 10, 15, tzinfo=UTC),
    )
    stop.save()
    body = _panel(logged_in_client, trip)
    # json_attr encodes with json.dumps' default ensure_ascii=True, so the middle dot comes
    # through as its \u escape, not the literal character.
    assert "&quot;label&quot;: &quot;UA 123 \\u00b7 6:15 AM EDT&quot;" in body
    assert "&quot;refresh_allowed_at&quot;" in body


def test_panel_hides_verify_when_not_configured(logged_in_client, settings):
    settings.AVIATIONSTACK_API_KEY = ""
    trip = _trip()
    _with_flight(trip)
    assert "enabled: false" in _panel(logged_in_client, trip)


def test_panel_hides_verify_when_airport_has_no_scheduled_service(logged_in_client, settings):
    """Andrews (ADW) has a real IATA code, so the old code-length gate would have offered
    Verify here — flights.lookup would then refuse it after the round trip. The drawer
    must never even show the button (spec 2026-08-29 finding 2)."""
    from apps.addresses.models import Airport

    settings.AVIATIONSTACK_API_KEY = "k"
    trip = _trip()
    stop = _with_flight(trip)
    stop.airport = Airport.objects.get(iata="ADW")
    stop.save()
    assert "enabled: true && false" in _panel(logged_in_client, trip)


def test_panel_hides_verify_for_a_private_tail_number(logged_in_client, settings):
    """A tail number has no scheduled flight number for aviationstack to look up — Verify
    must never be offered here either, even though IAD has scheduled service
    (2026-08-29 §3)."""
    from apps.addresses.models import Airline

    settings.AVIATIONSTACK_API_KEY = "k"
    trip = _trip()
    stop = _with_flight(trip, number="N561FX")
    stop.airline = Airline.objects.get(iata="N")
    stop.save()
    assert "enabled: true && false" in _panel(logged_in_client, trip)
    assert "N561FX" in _panel(logged_in_client, trip)


def test_panel_flight_join_adds_no_query(logged_in_client, django_assert_max_num_queries):
    trip = _trip(stops=[f"Stop {i}" for i in range(8)])
    _with_flight(trip)
    # Budget 10, not 9: selectors.in_house_options adds one query (the active-drivers read)
    # even when there are no drivers to show.
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))


def test_panel_verified_flight_join_adds_no_query(logged_in_client, django_assert_max_num_queries):
    from apps.reservations.factories import FlightFactory

    trip = _trip(stops=[f"Stop {i}" for i in range(8)])
    stop = _with_flight(trip)
    stop.flight = FlightFactory(
        airline=stop.airline,
        airport=stop.airport,
        flight_number="123",
        flight_date=date(2026, 8, 26),
        direction="arrival",
    )
    stop.save()
    # Budget 10, not 9: the in-house fleet merge added one query to the drawer
    # (selectors.in_house_options' active-drivers read). The flight join itself
    # is still free — that is what this test pins.
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))
