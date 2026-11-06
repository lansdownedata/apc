from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.dispatch import farm_in, farm_in_actions
from apps.dispatch.farm_in_actions import FarmInActionError
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.integrations import gnet
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead, VehicleType
from apps.leads.services import BookLeadError
from apps.payments.models import PaymentPlan
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


@pytest.fixture
def agent():
    return UserFactory()


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    vt.refresh_from_db()
    return vt


@pytest.fixture(autouse=True)
def airport_and_airline():
    AirportFactory(iata="LAX", name="Los Angeles International", timezone="America/Los_Angeles")
    AirlineFactory(iata="UA")


def _req(payload=None) -> FarmInRequest:
    farm_in.handle_new(payload or sample_payload(), transaction_id=TID, delivery_id="d-1")
    return FarmInRequest.objects.select_related("company").get(transaction_id=TID)


@pytest.fixture
def req(sedan):
    return _req()


def _accept(req, agent) -> tuple[FarmInRequest, Lead, Reservation]:
    out = farm_in_actions.accept(req.pk, user=agent)
    out.refresh_from_db()
    return out, out.lead, out.reservation


def test_accept_requires_price_and_vehicle(req, agent):
    FarmInRequest.objects.filter(pk=req.pk).update(quoted_amount=None)
    with pytest.raises(FarmInActionError, match="Price the request first") as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 400
    FarmInRequest.objects.filter(pk=req.pk).update(quoted_amount=Decimal("165"), vehicle=None)
    with pytest.raises(FarmInActionError, match="Choose a vehicle first") as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 400


def test_accept_books_a_lead_on_the_companys_contact(req, agent):
    contact = req.company.contacts.get()
    _, lead, _ = _accept(req, agent)
    assert lead.channel == Channel.GNET and lead.status == Lead.Status.BOOKED
    assert lead.contact == contact and lead.contact.company == req.company
    assert "Please confirm receipt" in lead.notes
    assert "GNet res no 1715016686" in lead.notes
    assert "Meet at curb" not in lead.notes


def test_accept_notes_carry_the_event_name(sedan, agent):
    payload = sample_payload()
    payload["eventName"] = "Smith Wedding"
    _, lead, _ = _accept(_req(payload), agent)
    assert lead.notes.splitlines() == [
        "Please confirm receipt",
        "Event: Smith Wedding",
        "GNet res no 1715016686",
    ]


def test_accept_creates_contact_when_company_has_none(req, agent):
    Contact.objects.filter(company=req.company).delete()
    _, lead, _ = _accept(req, agent)
    assert lead.contact.company == req.company
    assert lead.contact.name == req.company.name and lead.contact.channel == Channel.GNET


def test_accept_prices_the_trip_at_the_quote(req, agent):
    _, _, res = _accept(req, agent)
    assert res.rate == Decimal("165.00") and res.min_hours == Decimal("1")
    assert res.hours == Decimal("0") and res.trip_type == Reservation.TripType.TRANSFER
    assert res.line_total == Decimal("165.00") and res.booked_total == Decimal("165.00")
    assert res.vehicle == req.vehicle and res.passengers == 2


def test_accept_pushes_the_trips_line_total_not_the_quote(sedan, agent):
    VehicleType.objects.filter(pk=sedan.pk).update(hourly_min_hours=3)
    payload = sample_payload()
    payload["runType"] = "Hourly"
    req = _req(payload)
    farm_in_actions.price(req.pk, user=agent, amount=Decimal("100.00"))
    _, _, res = _accept(req, agent)
    assert res.trip_type == Reservation.TripType.HOURLY
    assert res.rate == Decimal("33.33") and res.min_hours == Decimal("3")
    assert res.line_total == Decimal("99.99")
    push = FarmInStatusPush.objects.get(request=req, status="CONFIRMED")
    assert push.total_amount == Decimal("99.99")


def test_accept_bills_one_hour_when_vehicle_has_no_minimum(sedan, agent):
    VehicleType.objects.filter(pk=sedan.pk).update(transfer_min_hours=0)
    req = _req()
    farm_in_actions.price(req.pk, user=agent, amount=Decimal("120"))
    _, _, res = _accept(req, agent)
    assert res.hours == Decimal("1") and res.min_hours == Decimal("0")
    assert res.rate == Decimal("120.00") and res.line_total == Decimal("120.00")


def test_accept_builds_stops_in_order_with_airport_and_flight(req, agent):
    _, _, res = _accept(req, agent)
    stops = list(res.stops.select_related("airport", "airline").order_by("sequence"))
    assert [s.sequence for s in stops] == [0, 1, 2]
    pickup, middle, dropoff = stops
    assert pickup.address.startswith("123 S Beverly Dr")
    assert pickup.latitude == Decimal("34.066470")
    assert pickup.longitude == Decimal("-118.399324")
    assert pickup.name == "Mr. Adam"
    assert "Carseat needed" in pickup.note and "Meet at curb" in pickup.note
    assert "Meet and greet" not in pickup.note
    assert pickup.airport is None and pickup.flight_direction == ""
    assert middle.address.startswith("9876 Wilshire")
    assert dropoff.airport.iata == "LAX" and dropoff.address.endswith("(LAX)")
    assert dropoff.airline.iata == "UA" and dropoff.flight_number == "123"
    assert dropoff.flight_direction == "departure"


def test_accept_orders_middle_stops_by_stop_number(sedan, agent):
    payload = sample_payload()
    payload["locations"]["stops"] = [
        {"address": "Second stop", "stopNumber": "2", "specialInstructions": "Ring bell"},
        {"address": "First stop", "stopNumber": "1"},
    ]
    payload["locations"]["pickup"]["meetAndGreet"] = "True"
    _, _, res = _accept(_req(payload), agent)
    stops = list(res.stops.order_by("sequence"))
    assert [s.address for s in stops[1:3]] == ["First stop", "Second stop"]
    assert stops[2].note == "Ring bell"
    assert stops[0].note.endswith("Meet and greet")


def test_accept_pickup_is_local_time_in_trip_zone(req, agent):
    _, _, res = _accept(req, agent)
    assert res.pickup_date == date(2024, 8, 3)
    assert res.pickup_time.strftime("%H:%M") == "10:27"
    assert res.pickup_timezone == "America/Los_Angeles"


def test_accept_creates_passengers_with_e164_mobile(sedan, agent):
    _, _, res = _accept(_req(), agent)
    (rider,) = res.named_passengers.all()
    expected = to_e164("+155550044444") or ""
    assert rider.name == "Alex Smith" and rider.sort == 0
    assert rider.mobile == expected and rider.is_lead == bool(expected)

    payload = sample_payload()
    payload["passengers"] = [
        {"firstName": "Dana", "lastName": "Lee", "phones": [{"number": "+12025550123"}]},
        {"firstName": " ", "lastName": ""},
        {"firstName": "Sam", "lastName": "Ray", "phoneNumber": "+12025550124"},
    ]
    other = _req_with_tid(payload, "tid-2")
    _, _, res = _accept(other, agent)
    riders = list(res.named_passengers.all())
    assert [r.name for r in riders] == ["Dana Lee", "Sam Ray"]
    assert riders[0].mobile == "+12025550123" and riders[0].is_lead is True
    assert riders[1].is_lead is False


def _req_with_tid(payload, tid) -> FarmInRequest:
    farm_in.handle_new(payload, transaction_id=tid, delivery_id="d-2")
    return FarmInRequest.objects.get(transaction_id=tid)


def test_accept_without_pickup_time_books_and_alerts(sedan, agent):
    payload = sample_payload()
    del payload["locations"]["pickup"]["time"]
    out, lead, res = _accept(_req(payload), agent)
    assert res.pickup_date is None and res.pickup_time is None
    assert out.status == FarmInRequest.Status.ACCEPTED
    assert lead.status == Lead.Status.BOOKED
    assert out.alert == "No pickup time: set it on the order"


def test_accept_marks_request_and_enqueues_confirmed(
    req, agent, settings, django_capture_on_commit_callbacks
):
    settings.GNET_API_KEY = "k"
    settings.GNET_GATEWAY_URL = "https://gw.test"
    with patch.object(gnet, "requests") as fake:
        resp = MagicMock(status_code=200, text="", content=b"x")
        resp.json.return_value = {"ok": True}
        fake.request.return_value = resp
        with django_capture_on_commit_callbacks(execute=True):
            farm_in_actions.accept(req.pk, user=agent)
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.ACCEPTED
    assert req.lead is not None and req.reservation.lead == req.lead
    assert req.decided_by == agent and req.decided_at is not None
    assert req.alert == ""
    push = FarmInStatusPush.objects.get(request=req)
    assert push.status == "CONFIRMED" and push.total_amount == Decimal("165.00")
    assert push.state == FarmInStatusPush.State.SENT


def test_accept_applies_the_farm_in_clock(req, agent):
    _, lead, _ = _accept(req, agent)
    assert lead.is_farm_in is True
    assert PaymentPlan.objects.filter(lead=lead).exists()


def test_accept_twice_second_gets_409(req, agent):
    farm_in_actions.accept(req.pk, user=agent)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 409
    assert Lead.objects.count() == 1


def test_accept_rolls_back_order_when_booking_fails(req, agent):
    with patch.object(farm_in_actions, "book_lead", side_effect=BookLeadError("No can do")):
        with pytest.raises(FarmInActionError, match="No can do"):
            farm_in_actions.accept(req.pk, user=agent)
    assert Lead.objects.count() == 0 and Reservation.objects.count() == 0
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.PENDING and req.lead is None
    assert not FarmInStatusPush.objects.exists()
