"""Trip detail, `/account/trips/<pk>/`: where one trip stands, its itinerary and its money."""

import html
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.customers.factories import CustomerAccountFactory
from apps.customers.trip_detail import trip_detail
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import (
    FlightFactory,
    PassengerFactory,
    ReservationFactory,
    StopFactory,
    TripExtraFactory,
)
from apps.reservations.models import (
    FlightDirection,
    Reservation,
    TripIssue,
    TripReview,
    TripStatusEvent,
)
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
LA = "America/Los_Angeles"
NOW = "django.utils.timezone.now"


def _url(trip: Reservation) -> str:
    return reverse("customers:trip", args=[trip.pk])


def _day(days: int):
    return timezone.localdate() + timedelta(days=days)


def _body(client, trip: Reservation) -> str:
    """The page as text, entities read back ("We're", not "We&#x27;re")."""
    response = client.get(_url(trip))
    assert response.status_code == 200
    return html.unescape(response.content.decode())


def _detail(account, trip: Reservation) -> dict:
    from apps.customers.selectors import trip_or_404

    return trip_detail(account, trip_or_404(account, trip.pk))


def _steps(detail: dict) -> list[tuple[str, str, str]]:
    return [(s["name"], s["date"], s["state"]) for s in detail["steps"]]


def _booked(account, make_trip, days: int = 30, **kw) -> Reservation:
    """Booked through `book_lead`, as staff or the deposit webhook do."""
    kw.setdefault("pickup_time", time(9, 10))
    trip = make_trip(account, lead__status=Lead.Status.QUOTED, pickup_date=_day(days), **kw)
    lead_services.book_lead(trip.lead)
    trip.refresh_from_db()
    return trip


def _paid(trip: Reservation, amount: Decimal, **kw) -> Charge:
    """A succeeded deposit of `amount` on the trip's order (its plan must exist)."""
    kw.setdefault("kind", Charge.Kind.DEPOSIT)
    plan = PaymentPlan.objects.get(lead_id=trip.lead_id)
    return ChargeFactory(plan=plan, amount=amount, status=Charge.Status.SUCCEEDED, **kw)


# --- scope ---------------------------------------------------------------------------------


def test_other_customers_trip_is_404(account, client_as, make_trip):
    client = client_as(account)
    theirs = make_trip(CustomerAccountFactory())
    wedding = make_trip(account, service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME))
    operator = ContactFactory(company=CompanyFactory(is_affiliate_account=True))
    farm_in = make_trip(CustomerAccountFactory(contact=operator))
    farm_in.lead.billing_contact = account.contact
    farm_in.lead.save(update_fields=["billing_contact"])

    for trip in (theirs, wedding, farm_in):
        assert client.get(_url(trip)).status_code == 404
    assert client.get(reverse("customers:trip", args=[theirs.pk + 1000])).status_code == 404
    assert client.get(_url(make_trip(account))).status_code == 200


def test_signed_out_goes_to_sign_in(account, make_trip):
    from django.test import Client

    trip = make_trip(account)
    response = Client().get(_url(trip))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("customers:login"))


def test_trips_and_home_rows_link_to_the_detail_page(account, client_as, make_trip):
    trip = _booked(account, make_trip)
    _paid(trip, trip.line_total)
    client = client_as(account)

    link = f'href="{_url(trip)}"'
    assert link in client.get(reverse("customers:trips")).content.decode()
    assert link in client.get(reverse("customers:home")).content.decode()


# --- driver --------------------------------------------------------------------------------


def test_driver_hidden_until_dispatched_and_affiliate_never_shown(account, client_as, make_trip):
    trip = _booked(account, make_trip)
    AssignmentFactory(
        reservation=trip,
        vendor__name="Blue Line Livery",
        status=Assignment.Status.CONFIRMED,
        driver_name="Sam Ortiz",
        driver_cell="+17035550199",
        vehicle_desc="Black Suburban",
    )
    set_trip_status(trip, S.ASSIGNED)
    client = client_as(account)

    body = _body(client, trip)
    assert _detail(account, trip)["driver"] is None
    assert "Sam Ortiz" not in body and "Blue Line" not in body
    assert "Your driver's name and cell appear here once assigned." in body

    set_trip_status(trip, S.DISPATCHED)
    body = _body(client, trip)
    driver = _detail(account, trip)["driver"]
    assert driver == {
        "name": "Sam Ortiz",
        "cell": {"tel": "+17035550199", "display": "(703) 555-0199"},
    }
    assert "Sam Ortiz" in body and 'href="tel:+17035550199"' in body
    assert "Blue Line" not in body and "appear here once assigned" not in body


# --- what never shows ----------------------------------------------------------------------


def test_internal_fields_absent(account, client_as, make_trip):
    marker = "ZZINTERNAL"
    trip = _booked(account, make_trip, affiliate_cost=Decimal("987.65"))
    _paid(trip, Decimal("100.00"), note=f"{marker} charge")
    lead = trip.lead
    lead.notes = f"{marker} lead notes"
    lead.lost_reason = f"{marker} lost"
    lead.save(update_fields=["notes", "lost_reason"])
    contact = lead.contact
    contact.notes = f"{marker} contact"
    contact.save(update_fields=["notes"])
    trip.cancel_reason = f"{marker} cancel"
    trip.driver_no_show_note = f"{marker} no show"
    trip.save(update_fields=["cancel_reason", "driver_no_show_note"])
    AssignmentFactory(
        reservation=trip,
        vendor__name=f"{marker} Livery",
        status=Assignment.Status.CONFIRMED,
        note=f"{marker} assignment",
        sent_back_reason=f"{marker} sent back",
        driver_name="Sam Ortiz",
    )
    review = TripReview.objects.create(reservation=trip, affiliate_overtime_note=f"{marker} ot")
    TripIssue.objects.create(
        review=review,
        category=TripIssue.Category.DRIVER,
        severity=TripIssue.Severity.LOW,
        note=f"{marker} issue",
    )
    set_trip_status(trip, S.DISPATCHED)

    body = _body(client_as(account), trip)

    assert marker not in body
    assert marker not in repr(_detail(account, trip))
    assert "affiliate" not in body.lower()
    assert str(trip.affiliate_cost) not in body and "987" not in body


def test_notes_for_the_driver_are_the_stop_notes(account, client_as, make_trip):
    trip = make_trip(account, stops=[])
    StopFactory(reservation=trip, sequence=0, address="1 Main St, Arlington, VA 22201")
    StopFactory(reservation=trip, sequence=1, name="Lincoln Memorial", note="20-min photo stop")
    StopFactory(reservation=trip, sequence=2, address="9 Elm St, Bethesda, MD 20814")

    assert _detail(account, trip)["notes"] == "20-min photo stop"
    body = _body(client_as(account), trip)
    assert "Notes for the driver" in body and "20-min photo stop" in body


def test_the_detail_holds_plain_values_only(account, make_trip):
    trip = _booked(account, make_trip)
    _paid(trip, Decimal("100.00"))

    def plain(value) -> bool:
        if isinstance(value, dict):
            return all(isinstance(k, str) and plain(v) for k, v in value.items())
        if isinstance(value, list | tuple):
            return all(plain(v) for v in value)
        return value is None or isinstance(value, str | int | bool | Decimal)

    assert plain(_detail(account, trip))


# --- progress line -------------------------------------------------------------------------


def _at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=UTC)


def test_steps_carry_dates_in_the_trips_own_zone(account, make_trip):
    """Each stamp is shown as the date it was in Los Angeles, not in UTC."""
    trip = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        pickup_date=_day(40),
        pickup_time=time(9, 10),
        pickup_timezone=LA,
    )
    lead = trip.lead
    lead.created_at = _at("2026-11-11", "05:00")  # Nov 10, 9 PM in LA
    lead.quote_sent_at = _at("2026-11-11", "06:00")
    lead.save(update_fields=["created_at", "quote_sent_at"])
    deposit = ChargeFactory(
        plan=PaymentPlanFactory(lead=lead),
        kind=Charge.Kind.DEPOSIT,
        status=Charge.Status.SUCCEEDED,
        authorized_at=_at("2026-11-13", "07:00"),  # Nov 12 in LA
    )
    with patch(NOW, return_value=_at("2026-11-13", "07:30")):
        lead_services.book_lead(lead)

    assert _steps(_detail(account, trip)) == [
        ("Requested", "Nov 10", "done"),
        ("Quote sent", "Nov 10", "done"),
        ("Deposit paid", "Nov 12", "done"),
        ("Confirmed", "Nov 12", "now"),
        ("Driver assigned", "", "todo"),
        ("Completed", "", "todo"),
    ]
    assert deposit.pk


def test_driver_assigned_and_completed_are_dated_from_the_status_history(account, make_trip):
    trip = _booked(account, make_trip, pickup_timezone=LA)
    _paid(trip, trip.line_total)
    with patch(NOW, return_value=_at("2026-12-02", "20:00")):
        set_trip_status(trip, S.DISPATCHED)
    with patch(NOW, return_value=_at("2026-12-04", "02:00")):  # Dec 3, 6 PM in LA
        set_trip_status(trip, S.DONE)

    steps = _steps(_detail(account, trip))

    assert steps[-2:] == [("Driver assigned", "Dec 2", "done"), ("Completed", "Dec 3", "now")]


def test_steps_for_on_account_order_skip_deposit(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(30))
    trip.lead.billing_group = AccountGroupFactory()
    trip.lead.save(update_fields=["billing_group"])
    lead_services.book_lead(trip.lead)

    names = [name for name, _, _ in _steps(_detail(account, trip))]

    assert "Deposit paid" not in names
    assert names[-3:] == ["Confirmed", "Driver assigned", "Completed"]


def test_a_request_shows_only_requested_done(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, rate=0, pickup_date=_day(20))

    steps = _steps(_detail(account, trip))

    assert steps[0][0] == "Requested" and steps[0][2] == "now" and steps[0][1]
    assert [(name, date, state) for name, date, state in steps[1:]] == [
        ("Quote sent", "", "todo"),
        ("Deposit paid", "", "todo"),
        ("Confirmed", "", "todo"),
        ("Driver assigned", "", "todo"),
        ("Completed", "", "todo"),
    ]


def test_a_quote_ready_trip_stands_at_quote_sent(account, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        lead__quote_sent_at=timezone.now(),
        lead__quote_expires_at=timezone.now() + timedelta(days=10),
    )

    states = [(name, state) for name, _, state in _steps(_detail(account, trip))]

    assert states[:3] == [("Requested", "done"), ("Quote sent", "now"), ("Deposit paid", "todo")]


def test_a_direct_booking_leaves_out_the_quote_and_deposit_it_never_had(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(30))
    lead_services.book_lead(trip.lead)

    assert [(name, state) for name, _, state in _steps(_detail(account, trip))] == [
        ("Requested", "done"),
        ("Confirmed", "now"),
        ("Driver assigned", "todo"),
        ("Completed", "todo"),
    ]


def test_a_cancelled_trip_shows_the_steps_reached_then_cancelled(account, make_trip):
    trip = _booked(account, make_trip)
    _paid(trip, Decimal("100.00"))
    trip.cancelled_at = timezone.now()
    trip.save(update_fields=["cancelled_at"])
    set_trip_status(trip, S.CANCELLED)

    steps = _steps(_detail(account, trip))

    assert [(name, state) for name, _, state in steps] == [
        ("Requested", "done"),
        ("Deposit paid", "done"),
        ("Confirmed", "done"),
        ("Cancelled", "now"),
    ]
    assert steps[-1][1]  # dated


def test_a_closed_quote_shows_the_steps_reached_then_closed(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.QUOTED, lead__quote_sent_at=timezone.now())
    lead_services.mark_lost(trip.lead, "")

    assert [(name, state) for name, _, state in _steps(_detail(account, trip))] == [
        ("Requested", "done"),
        ("Quote sent", "done"),
        ("Closed", "now"),
    ]


# --- money ---------------------------------------------------------------------------------


def _pay_target(url: str) -> tuple[str, int]:
    """(route name, lead pk) of a pay link. The token is timestamped, so a second one made
    in the test can differ from the page's by a second; read it back instead."""
    match = resolve(url)
    return match.url_name, lead_services.read_deposit_token(match.kwargs["token"]).pk


def test_quote_ready_trip_offers_review_and_pay(account, client_as, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        lead__quote_expires_at=timezone.now() + timedelta(days=10),
        pickup_date=_day(30),
    )

    detail = _detail(account, trip)
    assert _pay_target(detail["pay_url"]) == ("quote_page", trip.lead_id)
    assert detail["can_request"] is False and detail["call_dispatch"].startswith("Need to")
    response = client_as(account).get(_url(trip))
    body = response.content.decode()
    assert "Review and pay" in body and f'href="{response.context["trip"]["pay_url"]}"' in body


def test_a_booked_order_owing_its_deposit_offers_pay_now(account, client_as, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(30))
    lead_services.book_lead(trip.lead)

    assert _pay_target(_detail(account, trip)["pay_url"]) == ("quote_pay", trip.lead_id)
    assert "Pay now" in _body(client_as(account), trip)


def test_a_paid_up_trip_offers_no_payment(account, make_trip):
    trip = _booked(account, make_trip)
    plan = PaymentPlan.objects.get(lead_id=trip.lead_id)
    plan.deposit_status = PaymentPlan.DepositStatus.PAID
    plan.balance_status = PaymentPlan.BalanceStatus.PAID
    plan.save(update_fields=["deposit_status", "balance_status"])
    _paid(trip, trip.line_total)

    detail = _detail(account, trip)

    assert detail["pay_url"] is None
    assert detail["paid_line"] == "Paid in full"


def test_paid_line_follows_the_trips_own_money(account, make_trip):
    # Two trips on one order: the $100 deposit covers part of the first and none of the
    # second, so the order's "deposit paid" flag alone must not speak for the second.
    lead = LeadFactory(contact=account.contact, status=Lead.Status.QUOTED)
    first = ReservationFactory(lead=lead, rate=185, pickup_date=_day(10))
    second = ReservationFactory(lead=lead, rate=185, pickup_date=_day(20))
    lead_services.book_lead(lead)
    _paid(first, Decimal("100.00"))
    plan = PaymentPlan.objects.get(lead_id=lead.pk)
    plan.deposit_status = PaymentPlan.DepositStatus.PAID
    plan.save(update_fields=["deposit_status"])

    assert _detail(account, first)["paid_line"] == "Deposit paid"
    assert _detail(account, second)["paid_line"] == ""


def test_an_on_account_order_is_billed_to_the_account(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(30))
    trip.lead.billing_group = AccountGroupFactory()
    trip.lead.save(update_fields=["billing_group"])
    lead_services.book_lead(trip.lead)

    detail = _detail(account, trip)

    assert detail["paid_line"] == "Billed to your account"
    assert detail["pay_url"] is None


def test_charges_are_the_trips_price_lines_and_total(account, client_as, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.QUOTED, rate=170, hours=1, gratuity_pct=20)
    TripExtraFactory(reservation=trip, label="Airport fee", amount=10)
    trip.refresh_extras_total()

    detail = _detail(account, trip)

    assert [(line["label"], line["amount"]) for line in detail["price_lines"]] == [
        ("Base fare", Decimal("170.00")),
        ("Gratuity (20%)", Decimal("34.00")),
        ("Airport fee", Decimal("10.00")),
    ]
    assert detail["total"] == Decimal("214.00")
    body = _body(client_as(account), trip)
    assert "Gratuity (20%)" in body and "$214.00" in body


def test_a_draft_rate_on_a_new_lead_shows_no_charges_until_the_quote_is_sent(
    account, client_as, make_trip
):
    trip = make_trip(account, lead__status=Lead.Status.NEW, rate=170, hours=1, gratuity_pct=20)

    detail = _detail(account, trip)
    body = _body(client_as(account), trip)

    assert detail["total"] is None and detail["price_lines"] == []
    assert detail["paid_line"] == "" and detail["has_receipt"] is False
    assert "We're preparing your quote." in body
    assert "Base fare" not in body and "$204.00" not in body

    trip.lead.status = Lead.Status.QUOTED
    trip.lead.save(update_fields=["status"])
    body = _body(client_as(account), trip)
    assert "Base fare" in body and "$204.00" in body
    assert "We're preparing your quote." not in body


def test_a_cancelled_trip_charges_what_was_kept(account, make_trip):
    trip = _booked(account, make_trip)
    _paid(trip, Decimal("100.00"))
    set_trip_status(trip, S.LATE_CANCEL)

    detail = _detail(account, trip)

    assert [(line["label"], line["amount"]) for line in detail["price_lines"]] == [
        ("Cancellation charge", Decimal("100.00"))
    ]
    assert detail["total"] == Decimal("100.00")


# --- requests ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "days", "expected"),
    [(S.UNASSIGNED, 10, True), (S.DISPATCHED, 10, True), (S.ON_THE_WAY, 0, False)],
)
def test_can_request_until_the_trip_starts(account, make_trip, status, days, expected):
    trip = _booked(account, make_trip, days=days)
    set_trip_status(trip, status)

    assert _detail(account, trip)["can_request"] is expected


def test_a_trip_whose_pickup_has_passed_cannot_be_requested(account, make_trip):
    trip = _booked(account, make_trip, days=-1)

    assert _detail(account, trip)["can_request"] is False


def test_request_buttons_absent_once_trip_has_started(account, client_as, make_trip):
    started = _booked(account, make_trip, days=0)
    set_trip_status(started, S.ON_THE_WAY)
    upcoming = _booked(account, make_trip, days=10)
    client = client_as(account)

    later = ("Book this trip again", "Email receipt", "Download receipt")
    asks = ("Request a change", "Request to cancel")
    started_body, upcoming_body = _body(client, started), _body(client, upcoming)
    for text in (*later, *asks):
        assert text not in started_body
    for text in later:
        assert text not in upcoming_body
    for text in asks:
        assert text in upcoming_body


# --- rendering -----------------------------------------------------------------------------


def test_trip_with_no_stops_and_no_price_renders(account, client_as, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.NEW,
        stops=[],
        vehicle=None,
        rate=0,
        pickup_date=None,
        pickup_time=None,
        pickup_timezone="",
    )

    detail = _detail(account, trip)
    body = _body(client_as(account), trip)

    assert detail["stops"] == [] and detail["total"] is None and detail["price_lines"] == []
    assert "Date to be confirmed" in body
    assert "Vehicle to be confirmed" in body
    assert "We're preparing your quote." in body
    assert "$0.00" not in body


def test_times_show_trip_zone_abbreviation(account, client_as, make_trip):
    trip = make_trip(
        account,
        pickup_date=_day(30).replace(month=12, day=3),
        pickup_time=time(9, 10),
        pickup_timezone=LA,
    )

    body = _body(client_as(account), trip)

    assert "9:10 AM" in body and "PST" in body
    assert _detail(account, trip)["stops"][0]["detail"] == "Pickup 9:10 AM PST."


def test_itinerary_names_the_airport_and_meets_the_flight(account, client_as, make_trip):
    airport = AirportFactory(name="Reagan National Airport", iata="DCA")
    airline = AirlineFactory(iata="AA", name="American Airlines")
    day = _day(30)
    trip = make_trip(account, stops=[], pickup_date=day, pickup_time=time(9, 10))
    flight = FlightFactory(
        airline=airline,
        flight_number="2114",
        flight_date=day,
        airport=airport,
        direction=FlightDirection.ARRIVAL,
        scheduled_at=datetime.combine(day, time(13, 52), tzinfo=UTC),  # 8:52 AM EST
    )
    StopFactory(
        reservation=trip,
        sequence=0,
        address="2401 Smith Blvd, Arlington, VA 22202",
        airport=airport,
        airline=airline,
        flight_number="2114",
        flight_direction=FlightDirection.ARRIVAL,
        flight=flight,
    )
    StopFactory(reservation=trip, sequence=1, address="1100 Pennsylvania Ave NW, Washington, DC")
    trip.pickup_timezone = "America/New_York"
    trip.save(update_fields=["pickup_timezone"])

    stops = _detail(account, trip)["stops"]

    zone = trip.pickup_tz_label
    assert stops[0]["title"] == "Reagan National Airport (DCA)"
    assert stops[0]["detail"] == (
        f"Pickup 9:10 AM {zone}. American Airlines 2114, arriving 8:52 AM {zone}."
    )
    assert stops[1]["title"] == "1100 Pennsylvania Ave NW, Washington, DC"
    assert stops[1]["detail"] == "Drop-off"
    body = _body(client_as(account), trip)
    assert "Reagan National Airport (DCA)" in body and "American Airlines 2114" in body


def test_vehicle_and_passengers(account, client_as, make_trip):
    trip = make_trip(account, vehicle=VehicleTypeFactory(name="Executive SUV"), passengers=3)
    PassengerFactory(reservation=trip, name="Dana Whitfield", is_lead=True, sort=0)
    PassengerFactory(reservation=trip, name="Marcus Bell", sort=1)

    detail = _detail(account, trip)

    assert detail["vehicle"] == "Executive SUV"
    assert detail["passengers"] == ["Dana Whitfield", "Marcus Bell"]
    assert "Dana Whitfield, Marcus Bell" in _body(client_as(account), trip)


def test_your_reference_is_editable_and_saves_through_the_reference_endpoint(
    account, client_as, make_trip
):
    trip = make_trip(account, customer_reference="Halvorsen engagement")
    client = client_as(account)

    body = _body(client, trip)
    assert 'value="Halvorsen engagement"' in body
    assert reverse("customers:set_reference") in body
    response = client.post(
        reverse("customers:set_reference"), {"ids": str(trip.pk), "value": "Q4 offsite"}
    )
    assert response.json() == {"ok": True, "changed": 1}
    trip.refresh_from_db()
    assert trip.customer_reference == "Q4 offsite"


def test_expected_flight_at_trusts_only_a_flight_still_expected():
    from apps.customers.selectors import expected_flight_at
    from apps.reservations.models import Flight

    scheduled = FlightFactory(status=Flight.Status.SCHEDULED, estimated_at=None)
    estimated = timezone.now() + timedelta(hours=2)
    active = FlightFactory(status=Flight.Status.ACTIVE, estimated_at=estimated)
    cancelled = FlightFactory(status=Flight.Status.CANCELLED)

    assert expected_flight_at(scheduled) == scheduled.scheduled_at
    assert expected_flight_at(active) == estimated
    assert expected_flight_at(cancelled) is None
    assert expected_flight_at(None) is None


# --- flight times and open dates (deferred 24) ---------------------------------------------


def test_a_flight_time_always_carries_its_own_zone(account, make_trip):
    """The airport's zone, named even when it is the trip's: a time never stands bare."""
    airline = AirlineFactory(iata="UA", name="United Airlines")
    ohare = AirportFactory(name="O'Hare International Airport", iata="ORD")
    ohare.timezone = "America/Chicago"
    ohare.save(update_fields=["timezone"])
    day = _day(30)
    trip = make_trip(
        account, stops=[], pickup_date=day, pickup_time=time(9, 10), pickup_timezone=LA
    )
    flight = FlightFactory(
        airline=airline,
        flight_number="88",
        flight_date=day,
        airport=ohare,
        direction=FlightDirection.DEPARTURE,
        scheduled_at=datetime.combine(day, time(20, 5), tzinfo=UTC),
    )
    StopFactory(reservation=trip, sequence=0, address="1 Main St, Pasadena, CA 91101")
    StopFactory(
        reservation=trip,
        sequence=1,
        airport=ohare,
        airline=airline,
        flight_number="88",
        flight_direction=FlightDirection.DEPARTURE,
        flight=flight,
    )

    detail = _detail(account, trip)["stops"][1]["detail"]

    zone = flight.scheduled_at.astimezone(ZoneInfo("America/Chicago"))
    assert zone.tzname() in ("CST", "CDT")
    assert detail == f"Drop-off. United Airlines 88, departing {zone:%-I:%M %p} {zone.tzname()}."


def test_a_middle_stops_time_carries_the_zone_while_the_pickup_time_is_open(account, make_trip):
    trip = make_trip(account, stops=[], pickup_date=_day(30), pickup_time=None, pickup_timezone=LA)
    StopFactory(reservation=trip, sequence=0, address="1 Main St, Pasadena, CA 91101")
    StopFactory(reservation=trip, sequence=1, name="Griffith Observatory", scheduled_time=time(14))
    StopFactory(reservation=trip, sequence=2, address="9 Elm St, Burbank, CA 91501")

    stops = _detail(account, trip)["stops"]

    assert stops[0]["detail"] == "Pickup"
    assert stops[1]["detail"] == f"Stop 2:00 PM {trip.pickup_tz_label}."


def test_list_rows_put_no_zone_after_a_time_still_open(account, client_as, make_trip):
    """ "Time to be confirmed EST" names a zone for no time."""
    trip = make_trip(account, lead__status=Lead.Status.BOOKED, pickup_date=_day(9))
    make_trip(account, lead__status=Lead.Status.BOOKED)  # no date either
    PaymentPlanFactory(lead=trip.lead, deposit_status=PaymentPlan.DepositStatus.PAID)
    client = client_as(account)

    for url in (reverse("customers:trips") + "?tab=all", reverse("customers:home")):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get(url).content.decode()))
        assert "Time to be confirmed" in text
        assert not re.search(r"Time to be confirmed [A-Z]{2,4}\b", text), url
        assert "Date to be confirmed · Time to be confirmed" not in text
        assert "Date and time to be confirmed" in text


# --- Pay now follows the trip (deferred 50) ------------------------------------------------


def _order_of_two(account) -> tuple[Reservation, Reservation]:
    lead = LeadFactory(contact=account.contact, status=Lead.Status.QUOTED)
    first = ReservationFactory(lead=lead, rate=185, pickup_date=_day(10), pickup_time=time(9))
    second = ReservationFactory(lead=lead, rate=185, pickup_date=_day(20), pickup_time=time(9))
    lead_services.book_lead(lead)
    first.refresh_from_db()
    second.refresh_from_db()
    return first, second


def test_order_money_is_asked_on_the_trips_it_is_still_owed_for(account, make_trip):
    """One order, two trips, $185 paid: by the pickup-order split the first is paid in full
    and the second has nothing, so the order's unpaid money is asked on the second only."""
    first, second = _order_of_two(account)
    _paid(first, Decimal("185.00"))

    assert _detail(account, first)["paid_line"] == "Paid in full"
    assert _detail(account, first)["pay_url"] is None
    assert _pay_target(_detail(account, second)["pay_url"]) == ("quote_pay", second.lead_id)


def test_a_cancelled_trip_never_asks_for_its_siblings_money(account, make_trip):
    first, second = _order_of_two(account)
    first.cancelled_at = timezone.now()
    first.save(update_fields=["cancelled_at"])
    set_trip_status(first, S.CANCELLED)

    assert _detail(account, first)["pay_url"] is None
    assert _detail(account, second)["pay_url"] is not None


def test_a_trip_pay_link_shows_on_its_own_trip_only(account, make_trip):
    """A pay link sent for one trip is what the pay page charges: only that trip's page
    leads to it, whatever else the order owes."""
    first, second = _order_of_two(account)
    plan = PaymentPlan.objects.get(lead_id=first.lead_id)
    ChargeFactory(
        plan=plan,
        kind=Charge.Kind.TRIP,
        status=Charge.Status.PENDING,
        reservation=second,
        amount=second.line_total,
        stripe_client_secret="pi_secret",
    )

    assert _detail(account, first)["pay_url"] is None
    detail = _detail(account, second)
    assert _pay_target(detail["pay_url"]) == ("quote_pay", second.lead_id)
    assert detail["pay_label"] == "Pay now"


# --- one phrase for the deposit (deferred 51) ----------------------------------------------


def _confirming(account, make_trip, *, held: bool = True) -> Reservation:
    trip = make_trip(
        account,
        lead__status=Lead.Status.ENGAGED,
        lead__quote_sent_at=_at("2026-11-11", "06:00"),
        pickup_date=_day(30),
        pickup_time=time(9, 10),
        pickup_timezone=LA,
    )
    if held:
        ChargeFactory(
            plan=PaymentPlanFactory(lead=trip.lead),
            kind=Charge.Kind.DEPOSIT,
            status=Charge.Status.AUTHORIZED,
            authorized_at=_at("2026-11-13", "07:00"),  # Nov 12 in LA
        )
    return trip


def test_a_confirming_trip_stands_at_deposit_paid_and_says_so(account, client_as, make_trip):
    trip = _confirming(account, make_trip)

    detail = _detail(account, trip)

    assert selectors_key(trip) == "confirming"
    assert [(name, state) for name, _, state in _steps(detail)] == [
        ("Requested", "done"),
        ("Quote sent", "done"),
        ("Deposit paid", "now"),
        ("Confirmed", "todo"),
        ("Driver assigned", "todo"),
        ("Completed", "todo"),
    ]
    assert _steps(detail)[2][1] == "Nov 12"
    assert (detail["paid_line"], detail["paid_tone"]) == ("Deposit paid", "gold")
    assert detail["pay_url"] is None
    body = _body(client_as(account), trip)
    assert body.count("Deposit paid") == 2  # the step and the charges card
    assert "Deposit received" not in body


def test_a_confirming_trip_with_no_hold_on_record_claims_no_deposit(account, make_trip):
    trip = _confirming(account, make_trip, held=False)

    detail = _detail(account, trip)

    assert ("Deposit paid", "", "now") in _steps(detail)
    assert detail["paid_line"] == ""


def selectors_key(trip: Reservation) -> str:
    from apps.customers.selectors import label_key

    return label_key(trip)


# --- the charges card with no figure (deferred 52) -----------------------------------------


def _unpriced(account, make_trip, status: str, **kw) -> Reservation:
    return make_trip(account, lead__status=status, rate=0, pickup_date=_day(30), **kw)


def test_only_a_request_is_told_its_quote_is_being_prepared(account, client_as, make_trip):
    request = _unpriced(account, make_trip, Lead.Status.NEW)
    booked = _unpriced(account, make_trip, Lead.Status.BOOKED, trip_status=S.UNASSIGNED)
    billed = _unpriced(
        account,
        make_trip,
        Lead.Status.BOOKED,
        trip_status=S.UNASSIGNED,
        lead__billing_group=AccountGroupFactory(),
    )
    done = _unpriced(account, make_trip, Lead.Status.BOOKED, trip_status=S.DONE)
    closed = _unpriced(account, make_trip, Lead.Status.NEW)
    lead_services.mark_lost(closed.lead, "")

    notes = {
        name: _detail(account, trip)["charges_note"]
        for name, trip in (
            ("request", request),
            ("booked", booked),
            ("billed", billed),
            ("done", done),
            ("closed", closed),
        )
    }

    assert notes == {
        "request": "We're preparing your quote.",
        "booked": "We're confirming the price for this trip. It will show here once it's set.",
        "billed": "This trip is billed to your account. Its price will show here once it's set.",
        "done": "No charges to show for this trip.",
        "closed": "This request was closed before it was priced.",
    }
    body = _body(client_as(account), booked)
    assert "preparing your quote" not in body
    assert "We're confirming the price for this trip." in body


def test_a_priced_trip_has_no_charges_note(account, make_trip):
    assert _detail(account, _booked(account, make_trip))["charges_note"] == ""


# --- template hygiene (deferred 55) --------------------------------------------------------

_TEMPLATE = Path(settings.BASE_DIR) / "templates" / "customers" / "trip.html"


def test_a_trip_with_no_passenger_count_never_reads_zero_passengers(account, client_as, make_trip):
    trip = make_trip(account, passengers=0)

    assert _detail(account, trip)["passengers_text"] == "To be confirmed"
    assert "0 passengers" not in _body(client_as(account), trip)
    one = make_trip(account, passengers=1)
    assert _detail(account, one)["passengers_text"] == "1 passenger"


def test_the_driver_line_waits_for_a_booked_trip(account, client_as, make_trip):
    """A request or a quote has no driver coming yet: no "Driver" row at all."""
    request = _unpriced(account, make_trip, Lead.Status.NEW)
    quote = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        lead__quote_expires_at=timezone.now() + timedelta(days=10),
    )

    for trip in (request, quote):
        assert _detail(account, trip)["driver_pending"] == ""
        assert ">Driver<" not in _body(client_as(account), trip)
    booked = _booked(account, make_trip)
    assert _detail(account, booked)["driver_pending"]


def test_a_dispatched_trip_with_no_driver_on_file_does_not_say_once_assigned(account, make_trip):
    trip = _booked(account, make_trip)
    set_trip_status(trip, S.DISPATCHED)

    detail = _detail(account, trip)

    assert detail["label"] == "Driver assigned" and detail["driver"] is None
    assert detail["driver_pending"] == "Your driver's name and cell will appear here shortly."


def test_every_description_list_groups_its_terms_one_way(account, client_as, make_trip):
    """A `<dl>` holds `<dt>`/`<dd>` pairs or `<div>` groups of them, never both."""
    trip = make_trip(account, lead__status=Lead.Status.QUOTED, rate=170, hours=1, gratuity_pct=20)

    body = _body(client_as(account), trip)

    lists = re.findall(r"<dl[^>]*>(.*?)</dl>", body, re.S)
    assert len(lists) == 2
    for inner in lists:
        loose = re.sub(r"<div[^>]*>.*?</div>", "", inner, flags=re.S)
        assert "<dt" not in loose and "<dd" not in loose, inner


def test_no_empty_paragraphs(account, client_as, make_trip):
    for trip in (make_trip(account), _booked(account, make_trip)):
        body = _body(client_as(account), trip)
        page = body[body.index("<h1") : body.index("Prints on your receipt.")]
        # The request dialogs' live region (`role="alert"`) is empty until it speaks.
        assert not re.search(r'<p(?![^>]*role="alert")[^>]*>\s*</p>', page)


def test_the_page_keeps_to_four_type_sizes():
    """Small, body, section title, and the heading (22px, 28px from `sm` up)."""
    sizes = set(re.findall(r"text-\[([\d.]+)px\]", _TEMPLATE.read_text()))

    assert sizes == {"12.5", "13.5", "15", "22", "28"}


# --- your reference (deferred 56) ----------------------------------------------------------


def test_the_reference_field_saves_on_blur_so_a_failed_save_retries(account, client_as, make_trip):
    """`change` fires only when the text differs from what it was at focus, so after a
    failed save a second blur did nothing. Blur always asks; `save()` skips a saved value."""
    body = _body(client_as(account), make_trip(account))

    field = re.search(r'<input id="trip-reference"[^>]*>', body, re.S).group(0)
    assert '@blur="save()"' in field
    assert "@change" not in field


# --- step dates (deferred 48, 49) ----------------------------------------------------------


def test_confirmed_is_never_dated_after_driver_assigned(account, make_trip):
    """An order with no booking event (its trip was created already booked): a later
    release back to Unassigned by the system is not the day it was confirmed."""
    trip = make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        trip_status=S.UNASSIGNED,
        pickup_date=_day(30),
        pickup_time=time(9, 10),
    )
    with patch(NOW, return_value=_at("2026-12-02", "20:00")):
        set_trip_status(trip, S.DISPATCHED)
    with patch(NOW, return_value=_at("2026-12-05", "20:00")):
        set_trip_status(trip, S.UNASSIGNED, source=TripStatusEvent.Source.SYSTEM)
    with patch(NOW, return_value=_at("2026-12-06", "20:00")):
        set_trip_status(trip, S.DISPATCHED)

    steps = {name: (date, state) for name, date, state in _steps(_detail(account, trip))}

    assert steps["Confirmed"] == ("", "done")
    assert steps["Driver assigned"] == ("Dec 2", "now")


def test_confirmed_falls_back_to_the_plan_when_the_event_is_a_later_release(account, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        trip_status=S.UNASSIGNED,
        pickup_date=_day(30),
        pickup_time=time(9, 10),
    )
    plan = PaymentPlanFactory(lead=trip.lead)
    PaymentPlan.objects.filter(pk=plan.pk).update(created_at=_at("2026-11-30", "20:00"))
    with patch(NOW, return_value=_at("2026-12-02", "20:00")):
        set_trip_status(trip, S.DISPATCHED)
    with patch(NOW, return_value=_at("2026-12-05", "20:00")):
        set_trip_status(trip, S.UNASSIGNED, source=TripStatusEvent.Source.SYSTEM)

    steps = {name: date for name, date, _ in _steps(_detail(account, trip))}

    assert steps["Confirmed"] == "Nov 30"


def test_the_deposit_is_dated_when_its_money_was_booked_never_when_the_row_last_changed(
    account, make_trip
):
    """A deposit paid outright has no hold or capture time. The ledger entry posted for it
    is when it was paid; with none, the step stands undated rather than guessed."""
    trip = _booked(account, make_trip, pickup_timezone=LA)
    deposit = _paid(trip, Decimal("100.00"))
    Charge.objects.filter(pk=deposit.pk).update(updated_at=_at("2026-12-20", "20:00"))

    assert ("Deposit paid", "", "done") in _steps(_detail(account, trip))

    entry = JournalEntry.objects.create(
        lead=trip.lead,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        charge=deposit,
        idempotency_key=f"capture-charge{deposit.pk}",
    )
    JournalEntry.objects.filter(pk=entry.pk).update(posted_at=_at("2026-11-13", "07:00"))

    assert ("Deposit paid", "Nov 12", "done") in _steps(_detail(account, trip))


# --- queries (deferred 145) ----------------------------------------------------------------


def _busy_trip(account, make_trip, stops: int) -> Reservation:
    """A dispatched trip with a driver, an extra, a paid deposit and `stops` stops, each
    with a note and a named passenger."""
    trip = _booked(account, make_trip, pickup_timezone=LA, stops=[])
    _paid(trip, Decimal("100.00"))
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, driver_name="Sam Ortiz")
    set_trip_status(trip, S.DISPATCHED)
    TripExtraFactory(reservation=trip, label="Airport fee", amount=10)
    trip.refresh_extras_total()
    for i in range(stops):
        StopFactory(reservation=trip, sequence=i, note=f"Stop {i}", address=f"{i} Main St")
        PassengerFactory(reservation=trip, name=f"Rider {i}")
    return trip


def _queries(client, trip: Reservation) -> list[str]:
    with CaptureQueriesContext(connection) as captured:
        assert client.get(_url(trip)).status_code == 200
    return [q["sql"] for q in captured.captured_queries]


def test_the_page_costs_the_same_whatever_the_trip_holds(account, client_as, make_trip):
    small = _busy_trip(account, make_trip, stops=2)
    large = _busy_trip(account, make_trip, stops=12)
    client = client_as(account)

    few, many = _queries(client, small), _queries(client, large)

    assert len(few) == len(many)
    # Measured 29: the session and account (3), the trip with its stops and passengers (3),
    # the order's trips (1), the money split (4), the driver (1), the deposit and when it
    # was posted (2), the status history (1), the pay page's ledger reads for a booked
    # order (12), the extras (1) and the open requests (1).
    assert len(many) <= 30
    # The stops come with the trip (`visible_trips`' prefetch) and are never read again.
    stop_reads = [sql for sql in many if re.search(r"FROM .reservations_stop.", sql)]
    assert len(stop_reads) == 1


def test_flights_cost_the_same_for_one_stop_or_many(account, client_as, make_trip):
    airline = AirlineFactory(iata="AA", name="American Airlines")

    def with_flights(n: int) -> Reservation:
        trip = _busy_trip(account, make_trip, stops=0)
        for i in range(n):
            airport = AirportFactory()
            flight = FlightFactory(airline=airline, flight_number=str(100 + i), airport=airport)
            StopFactory(
                reservation=trip,
                sequence=i,
                airport=airport,
                airline=airline,
                flight_number=str(100 + i),
                flight=flight,
            )
        return trip

    client = client_as(account)

    assert len(_queries(client, with_flights(1))) == len(_queries(client, with_flights(6)))
