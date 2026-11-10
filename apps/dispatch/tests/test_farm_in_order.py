"""Arrival builds the order: a farm-in request becomes a lead with one priced trip."""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.contacts.factories import CompanyFactory
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.dispatch import farm_in, farm_in_order
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead, VehicleType
from apps.payments.models import PaymentPlan
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(
        rate=Decimal("165"), transfer_min_hours=1, hourly_min_hours=3
    )
    vt.refresh_from_db()
    return vt


@pytest.fixture(autouse=True)
def airport_and_airline():
    AirportFactory(iata="LAX", name="Los Angeles International", timezone="America/Los_Angeles")
    AirlineFactory(iata="UA")


def _req(payload=None, tid=TID) -> FarmInRequest:
    farm_in.handle_new(payload or sample_payload(), transaction_id=tid, delivery_id="d-1")
    return FarmInRequest.objects.select_related("company", "lead", "reservation").get(
        transaction_id=tid
    )


def test_build_puts_a_new_lead_on_the_companys_contact(sedan):
    req = _req()
    lead = req.lead
    assert lead.channel == Channel.GNET and lead.status == Lead.Status.NEW
    assert lead.contact == req.company.contacts.get()
    assert "Please confirm receipt" in lead.notes
    assert "GNet res no 1715016686" in lead.notes
    assert "Meet at curb" not in lead.notes
    assert req.reservation.lead == lead
    assert not PaymentPlan.objects.filter(lead=lead).exists()


def test_build_notes_carry_the_event_name(sedan):
    payload = sample_payload()
    payload["eventName"] = "Smith Wedding"
    assert _req(payload).lead.notes.splitlines() == [
        "Please confirm receipt",
        "Event: Smith Wedding",
        "GNet res no 1715016686",
    ]


def test_build_creates_contact_when_company_has_none(sedan):
    company = CompanyFactory(name="GNet Test", gnet_grid_id="gnettest")
    Contact.objects.filter(company=company).delete()
    lead = _req().lead
    assert lead.contact.company == company
    assert lead.contact.name == company.name and lead.contact.channel == Channel.GNET


def test_build_prices_from_rate_card_not_a_quote(sedan):
    res = _req().reservation
    assert res.vehicle == sedan and res.trip_type == Reservation.TripType.TRANSFER
    assert res.rate == Decimal("165.00") and res.min_hours == Decimal("1")
    assert res.hours == Decimal("0") and res.line_total == Decimal("165.00")
    assert res.passengers == 2

    VehicleType.objects.filter(pk=sedan.pk).update(transfer_min_hours=4)
    res = _req(tid="tid-long-minimum").reservation
    assert res.rate == Decimal("165.00") and res.min_hours == Decimal("4")
    assert res.line_total == Decimal("660.00")

    payload = sample_payload()
    payload["runType"] = "Hourly"
    res = _req(payload, tid="tid-hourly").reservation
    assert res.trip_type == Reservation.TripType.HOURLY
    assert res.min_hours == Decimal("3") and res.line_total == Decimal("495.00")


def test_build_stops_in_order_with_airport_and_flight(sedan):
    res = _req().reservation
    stops = list(res.stops.select_related("airport", "airline").order_by("sequence"))
    assert [s.sequence for s in stops] == [0, 1, 2]
    pickup, middle, dropoff = stops
    assert pickup.address.startswith("123 S Beverly Dr")
    assert pickup.latitude == Decimal("34.066470")
    assert pickup.longitude == Decimal("-118.399324")
    # GNET-05: the name sign is a note for the driver, not the pickup's venue name, which
    # the trip line shows in place of the address.
    assert pickup.name == ""
    assert pickup.note.startswith("Sign: Mr. Adam · ")
    assert "Carseat needed" in pickup.note and "Meet at curb" in pickup.note
    assert "Meet and greet" not in pickup.note
    assert pickup.airport is None and pickup.flight_direction == ""
    assert middle.address.startswith("9876 Wilshire")
    assert dropoff.airport.iata == "LAX" and dropoff.address.endswith("(LAX)")
    assert dropoff.airline.iata == "UA" and dropoff.flight_number == "123"
    assert dropoff.flight_direction == "departure"


def test_build_orders_middle_stops_by_stop_number(sedan):
    payload = sample_payload()
    payload["locations"]["stops"] = [
        {"address": "Second stop", "stopNumber": "2", "specialInstructions": "Ring bell"},
        {"address": "First stop", "stopNumber": "1"},
    ]
    payload["locations"]["pickup"]["meetAndGreet"] = "True"
    payload["locations"]["pickup"]["specialInstructions"] = "Gate code 12"
    stops = list(_req(payload).reservation.stops.order_by("sequence"))
    assert [s.address for s in stops[1:3]] == ["First stop", "Second stop"]
    assert stops[2].note == "Ring bell"
    assert stops[0].note == (
        "Sign: Mr. Adam · - Carseat needed · Gate code 12 · Meet at curb · Meet and greet"
    )


def test_build_pickup_is_local_time_in_trip_zone(sedan):
    res = _req().reservation
    assert res.pickup_date == date(2024, 8, 3)
    assert res.pickup_time == time(10, 27)
    assert res.pickup_timezone == "America/Los_Angeles"


def test_build_creates_passengers_with_e164_mobile(sedan):
    (rider,) = _req().reservation.named_passengers.all()
    expected = to_e164("+155550044444") or ""
    assert rider.name == "Alex Smith" and rider.sort == 0
    assert rider.mobile == expected and rider.is_lead == bool(expected)

    payload = sample_payload()
    payload["passengers"] = [
        {"firstName": "Dana", "lastName": "Lee", "phones": [{"number": "+12025550123"}]},
        {"firstName": " ", "lastName": ""},
        {"firstName": "Sam", "lastName": "Ray", "phoneNumber": "+12025550124"},
    ]
    riders = list(_req(payload, tid="tid-2").reservation.named_passengers.all())
    assert [r.name for r in riders] == ["Dana Lee", "Sam Ray"]
    assert riders[0].mobile == "+12025550123" and riders[0].is_lead is True
    assert riders[1].is_lead is False


def test_build_without_pickup_time_leaves_the_date_blank(sedan):
    payload = sample_payload()
    del payload["locations"]["pickup"]["time"]
    req = _req(payload)
    assert req.reservation.pickup_date is None and req.reservation.pickup_time is None
    assert farm_in.ALERT_NO_PICKUP in req.alert


def test_build_keeps_a_blank_address_pickup_as_stop_zero(sedan):
    payload = sample_payload()
    payload["locations"]["pickup"]["address"] = ""
    res = _req(payload).reservation
    stops = list(res.stops.select_related("airport").order_by("sequence"))
    assert len(stops) == 3
    assert stops[0].address == "" and stops[0].name == ""
    assert "Sign: Mr. Adam" in stops[0].note
    assert "Carseat needed" in stops[0].note and "Meet at curb" in stops[0].note
    assert stops[0].flight_direction == ""
    assert stops[-1].airport.iata == "LAX" and stops[-1].flight_direction == "departure"
    assert res.pickup_timezone == "America/Los_Angeles"


def test_build_rederives_local_pickup_when_the_stop_resolves_another_zone(sedan):
    payload = sample_payload()
    payload["locations"]["pickup"]["time"] = "2024-08-03T22:30:00"
    with patch("apps.reservations.timezones.resolve", return_value="America/New_York"):
        req = _req(payload)
    assert req.pickup_at == datetime(2024, 8, 4, 5, 30, tzinfo=UTC)
    res = req.reservation
    assert res.pickup_timezone == "America/New_York"
    assert res.pickup_date == date(2024, 8, 4) and res.pickup_time == time(1, 30)


def test_build_fetches_airports_and_airlines_once(sedan):
    payload = sample_payload()
    payload["locations"]["pickup"] = {
        "address": "LAX",
        "locationType": "airport",
        "time": "2024-08-03T10:27:22",
        "flightInfo": {"flightNumber": "9", "airlineCode": "UA"},
    }
    summary = farm_in.summarize(payload)
    company = CompanyFactory(name="GNet Test", gnet_grid_id="gnettest")
    with CaptureQueriesContext(connection) as ctx:
        farm_in_order.build(payload, company, summary=summary)
    sql = [q["sql"] for q in ctx.captured_queries]
    assert sum("FROM `addresses_airport`" in q for q in sql) == 1
    assert sum("FROM `addresses_airline`" in q for q in sql) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [("two", 1), ("2.0", 1), ("2+1", 1), ("0", 1), ("-3", 1), ({"n": 2}, 1), ([2], 1), (" 3 ", 3)],
)
def test_headcount_is_tolerant(raw, expected):
    p = sample_payload()
    p["passengerCount"] = raw
    assert farm_in_order._headcount(p) == expected


def test_malformed_passengers_do_not_raise():
    p = sample_payload()
    p["passengers"] = ["Alex"]
    assert farm_in_order._passenger_rows(p) == [] and farm_in_order._headcount(p) == 2
    p["passengers"] = "Alex"
    assert farm_in_order._passenger_rows(p) == []


def _locks_the_reservation(queries) -> bool:
    table = Reservation._meta.db_table
    return any(
        "FOR UPDATE" in q["sql"] and table in q["sql"] and q["sql"].lstrip().startswith("SELECT")
        for q in queries
    )


def test_apply_update_locks_the_reservation_row_before_writing(sedan):
    """A requester update and an editor save must not interleave on the trip row."""
    req = _req()
    payload = sample_payload()
    with CaptureQueriesContext(connection) as ctx:
        farm_in_order.apply_update(
            req.reservation, req.lead, payload, summary=farm_in.summarize(payload)
        )
    assert _locks_the_reservation(ctx.captured_queries)
    first_lock = next(i for i, q in enumerate(ctx.captured_queries) if "FOR UPDATE" in q["sql"])
    first_res_write = next(
        i
        for i, q in enumerate(ctx.captured_queries)
        if q["sql"].startswith("UPDATE") and Reservation._meta.db_table in q["sql"]
    )
    assert first_lock < first_res_write
