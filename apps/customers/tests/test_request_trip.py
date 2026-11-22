"""Request a trip, `/account/request/`: a signed-in customer's request becomes a New lead."""

import threading
import time as clock
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.addresses.models import Airline, Airport
from apps.audit.models import AuditEntry
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.core.choices import Channel
from apps.customers import requests
from apps.customers.factories import CustomerAccountFactory
from apps.customers.forms import TripRequestForm
from apps.customers.selectors import visible_trips
from apps.customers.services import CustomerError
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.messaging import events
from apps.messaging.models import TouchPoint
from apps.notifications.models import Notification
from apps.public.forms import BookingRequestForm
from apps.public.services import WEDDING_SERVICE_NAME, wedding_service_type
from apps.reservations.factories import FlightFactory, ReservationFactory, StopFactory
from apps.reservations.models import Passenger, Reservation

pytestmark = pytest.mark.django_db

URL = "/account/request/"
NOW = "django.utils.timezone.now"


def _day(days: int = 10) -> date:
    return timezone.localdate() + timedelta(days=days)


def _post(**overrides: object) -> dict:
    data = {
        "trip_type": "transfer",
        "pickup_date": _day().isoformat(),
        "pickup_time": "06:00",
        "pickup": "2200 Clarendon Blvd, Arlington, VA",
        "pickup_lat": "38.8869",
        "pickup_lng": "-77.0947",
        "dropoff": "1100 Pennsylvania Ave NW, Washington, DC",
        "dropoff_lat": "38.8941",
        "dropoff_lng": "-77.0280",
        "passengers": "2",
        "rider": "me",
        "notes": "Two large bags",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def _cleaned(**overrides: object) -> dict:
    form = TripRequestForm(_post(**overrides))
    assert form.is_valid(), form.errors
    return form.cleaned_data


def _account(**contact: object):
    return CustomerAccountFactory(
        contact=ContactFactory(**{"name": "Dana Whitfield", "phone": "+17035550188", **contact})
    )


# --- the lead it makes -----------------------------------------------------------------


def test_request_creates_new_portal_lead_on_my_contact(client_as):
    account = _account()
    response = client_as(account).post(URL, _post())

    lead = Lead.objects.get()
    trip = lead.reservations.get()
    assert response.status_code == 302
    assert response.url == reverse("customers:trip", args=[trip.pk])
    assert lead.contact == account.contact
    assert lead.status == Lead.Status.NEW
    assert lead.channel == Channel.PORTAL
    assert lead.notes == "Two large bags"
    assert trip.pickup_date == _day() and trip.pickup_time == time(6, 0)
    assert trip.passengers == 2
    assert trip.trip_type == Reservation.TripType.TRANSFER
    assert [s.address for s in trip.stops.order_by("sequence")] == [
        "2200 Clarendon Blvd, Arlington, VA",
        "1100 Pennsylvania Ave NW, Washington, DC",
    ]
    assert trip.pickup_timezone == "America/New_York"


def test_request_does_not_touch_contact_name_or_phone(client_as):
    account = _account()
    before = (account.contact.name, account.contact.phone, account.contact.email)
    client_as(account).post(URL, _post(name="Someone Else", phone="+12025550100"))

    account.contact.refresh_from_db()
    assert (account.contact.name, account.contact.phone, account.contact.email) == before
    assert Lead.objects.get().contact_id == account.contact_id


def test_rider_me_is_the_contact_with_their_texting_number():
    account = _account()
    lead = requests.request_trip(account, _cleaned())

    rider = lead.reservations.get().named_passengers.get()
    assert (rider.name, rider.mobile, rider.is_lead) == ("Dana Whitfield", "+17035550188", True)


# --- "Me" with no mobile on file: asked for one, kept, and made the lead passenger --------


@pytest.mark.parametrize("on_file", ["", "555-0100 ext 4"], ids=["none", "cannot be dialled"])
def test_me_without_a_usable_mobile_is_asked_for_one(client_as, on_file):
    account = _account(phone=on_file)
    client = client_as(account)
    page = client.get(URL).content.decode()
    assert 'name="my_mobile"' in page and "We don't have a mobile for you yet." in page

    response = client.post(URL, _post())

    assert response.status_code == 200 and Lead.objects.count() == 0
    assert response.context["form"].errors["my_mobile"] == [
        "Enter your mobile, so your driver can reach you."
    ]
    assert "Enter your mobile, so your driver can reach you." in response.content.decode()


def test_me_with_the_mobile_they_typed_is_the_lead_passenger_and_the_contact_keeps_it(client_as):
    account = _account(phone="")
    response = client_as(account).post(URL, _post(my_mobile="(703) 555-0188"), follow=True)

    rider = Lead.objects.get().reservations.get().named_passengers.get()
    assert (rider.name, rider.mobile, rider.is_lead) == ("Dana Whitfield", "+17035550188", True)
    account.contact.refresh_from_db()
    assert account.contact.phone == "+17035550188"
    assert account.contact.phones.get().texting is True
    # ... so the next request does not ask again, and the quote can now be texted
    assert "Your quote will arrive by email and text." in response.content.decode()
    assert 'name="my_mobile"' not in client_as(account).get(URL).content.decode()


def test_a_bad_typed_mobile_is_an_error_beside_it():
    form = TripRequestForm(_post(my_mobile="12"), needs_mobile=True)
    assert not form.is_valid()
    assert form.errors["my_mobile"] == ["Enter a valid mobile number."]


def test_a_customer_with_a_mobile_is_never_asked_and_a_posted_one_is_ignored(client_as):
    account = _account()
    client = client_as(account)
    assert 'name="my_mobile"' not in client.get(URL).content.decode()
    client.post(URL, _post(my_mobile="(202) 555-0111"))
    account.contact.refresh_from_db()
    assert account.contact.phone == "+17035550188"
    assert Lead.objects.get().reservations.get().named_passengers.get().mobile == "+17035550188"


def test_booking_for_someone_else_needs_no_mobile_of_my_own(client_as):
    account = _account(phone="")
    response = client_as(account).post(
        URL, _post(rider="other", rider_name="Sam Exec", rider_mobile="(202) 555-0142"), follow=True
    )
    rider = Lead.objects.get().reservations.get().named_passengers.get()
    assert (rider.name, rider.is_lead) == ("Sam Exec", True)
    account.contact.refresh_from_db()
    assert account.contact.phone == ""
    # nobody without a mobile is promised a text
    html = response.content.decode()
    assert "Your quote will arrive by email." in html and "email and text" not in html


def test_the_page_promises_a_text_only_to_a_mobile_on_file(client_as):
    with_mobile = client_as(_account()).get(URL).content.decode()
    assert "Your quote arrives by email and text, and shows here under Trips." in with_mobile
    without = client_as(_account(phone="")).get(URL).content.decode()
    assert "Your quote arrives by email, and shows here under Trips." in without


def test_rider_other_becomes_lead_passenger_with_mobile(client_as):
    account = _account()
    client_as(account).post(
        URL, _post(rider="other", rider_name="Sam Exec", rider_mobile="(202) 555-0142")
    )

    rider = Lead.objects.get().reservations.get().named_passengers.get()
    assert (rider.name, rider.mobile, rider.is_lead) == ("Sam Exec", "+12025550142", True)


def test_rider_other_without_mobile_is_a_form_error(client_as):
    response = client_as(_account()).post(URL, _post(rider="other", rider_name="Sam Exec"))

    assert response.status_code == 200
    assert "rider_mobile" in response.context["form"].errors
    assert Lead.objects.count() == 0


def test_rider_other_with_a_bad_mobile_or_no_name_is_a_form_error():
    form = TripRequestForm(_post(rider="other", rider_mobile="12"))
    assert not form.is_valid()
    assert set(form.errors) == {"rider_name", "rider_mobile"}


def test_reference_and_vehicle_preference_are_stored():
    suv = VehicleTypeFactory(name="Executive SUV")
    account = _account()
    lead = requests.request_trip(
        account, _cleaned(customer_reference="  PO 4471 ", vehicle_preference=str(suv.pk))
    )

    trip = lead.reservations.get()
    assert trip.customer_reference == "PO 4471"
    assert trip.vehicle_id is None  # staff still choose the vehicle
    assert lead.notes == "Two large bags\n\nVehicle preference: Executive SUV"


def test_reference_longer_than_120_is_a_form_error():
    form = TripRequestForm(_post(customer_reference="x" * 121))
    assert not form.is_valid() and "customer_reference" in form.errors


def test_airport_pickup_links_flight_and_sets_timezone():
    lax = Airport.objects.get(iata="LAX")
    united = Airline.objects.get(iata="UA")
    cached = FlightFactory(
        airline=united, airport=lax, flight_number="915", flight_date=_day(), direction="arrival"
    )
    lead = requests.request_trip(
        _account(),
        _cleaned(
            pickup="Los Angeles International Airport (LAX)",
            pickup_lat="33.9416",
            pickup_lng="-118.4085",
            pickup_airport=str(lax.pk),
            pickup_airline=str(united.pk),
            pickup_flight="915",
            dropoff="Santa Monica Pier",
            dropoff_lat="34.0094",
            dropoff_lng="-118.4973",
        ),
    )

    trip = lead.reservations.get()
    pickup = trip.stops.order_by("sequence").first()
    assert pickup.flight_direction == "arrival"
    assert pickup.flight_id == cached.pk
    assert trip.pickup_timezone == "America/Los_Angeles"


def test_stops_are_audited_under_the_customer_portal(client_as):
    client_as(_account()).post(URL, _post())

    trip = Reservation.objects.get()
    entries = AuditEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Reservation), object_id=trip.pk
    )
    assert {entry.source for entry in entries} == {"customer portal"}
    children = [entry.changes for entry in entries if "route.1" in entry.changes]
    assert len(children) == 1 and "named_passengers" in children[0]


# --- double clicks and the hourly limit -------------------------------------------------


def test_double_submit_creates_one_lead(client_as):
    client = client_as(_account())
    first = client.post(URL, _post())
    second = client.post(URL, _post())

    assert Lead.objects.count() == 1
    assert first.url == second.url


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_two_submits_at_the_same_moment_create_one_lead(restore_seeded_rows):
    """Two requests in flight together, each on its own connection. The first to lock the
    account looks for a duplicate, finds none and is slow to write; the lock is what makes
    the second wait and then find the first one's lead, instead of also finding none."""
    account = _account()
    data = _cleaned()
    looked = requests._duplicate
    barrier = threading.Barrier(2)
    leads, errors = [], []

    def slow_look(*args: object) -> Lead | None:
        found = looked(*args)
        clock.sleep(0.4)  # the gap a double click falls into
        return found

    def submit() -> None:
        try:
            barrier.wait()
            leads.append(requests.request_trip(account, dict(data)).pk)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connection.close()

    with patch.object(requests, "_duplicate", slow_look):
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert errors == []
    assert Lead.objects.count() == 1 and Reservation.objects.count() == 1
    assert leads[0] == leads[1]


def test_same_trip_after_thirty_seconds_is_a_new_request():
    account = _account()
    first = requests.request_trip(account, _cleaned())
    with patch(NOW, return_value=timezone.now() + timedelta(seconds=31)):
        second = requests.request_trip(account, _cleaned())
    assert first != second


def test_eleventh_request_in_an_hour_is_refused(client_as):
    account = _account()
    start = timezone.now()
    for minute in range(requests.REQUEST_LIMIT):
        with patch(NOW, return_value=start + timedelta(minutes=minute)):
            requests.request_trip(account, _cleaned(pickup_time=f"{6 + minute:02d}:00"))

    with patch(NOW, return_value=start + timedelta(minutes=30)):
        with pytest.raises(CustomerError, match="limited"):
            requests.request_trip(account, _cleaned(pickup_time="20:00"))
        response = client_as(account).post(URL, _post(pickup_time="20:00"))
    assert response.status_code == 200
    assert "a lot of requests" in response.content.decode()
    assert Lead.objects.count() == requests.REQUEST_LIMIT

    with patch(NOW, return_value=start + timedelta(hours=1, seconds=1)):
        requests.request_trip(account, _cleaned(pickup_time="20:00"))
    assert Lead.objects.count() == requests.REQUEST_LIMIT + 1


def test_the_limit_is_per_account():
    mine = _account()
    theirs = CustomerAccountFactory(contact=ContactFactory(phone="+12025550170"))
    for hour in range(requests.REQUEST_LIMIT):
        requests.request_trip(theirs, _cleaned(pickup_time=f"{6 + hour:02d}:00"))
    with pytest.raises(CustomerError, match="limited"):
        requests.request_trip(theirs, _cleaned(pickup_time="20:00"))

    lead = requests.request_trip(mine, _cleaned())  # their ten do not count against me

    assert lead.contact_id == mine.contact_id
    assert Lead.objects.filter(contact=theirs.contact).count() == requests.REQUEST_LIMIT
    assert Lead.objects.filter(contact=mine.contact).count() == 1


# --- staff and messages -----------------------------------------------------------------


def test_staff_are_notified():
    account = _account()
    lead = requests.request_trip(account, _cleaned())

    note = Notification.objects.get(lead=lead)
    assert note.kind == Notification.Kind.NEW_LEAD
    assert note.title == "New portal request: Dana Whitfield"


def test_website_worded_followup_is_not_scheduled_for_portal_lead():
    portal = LeadFactory(channel=Channel.PORTAL)
    website = LeadFactory(channel=Channel.WEBSITE)
    events.emit("lead_created", portal)
    events.emit("lead_created", website)

    def kinds(lead: Lead) -> set[str]:
        return set(TouchPoint.objects.filter(lead=lead).values_list("kind", flat=True))

    # TP1 thanks the customer "for taking the time to visit our website"; TP2 does not.
    assert kinds(portal) == {TouchPoint.Kind.TP2_LEAD_FOLLOWUP}
    assert kinds(website) == {TouchPoint.Kind.TP1_WELCOME, TouchPoint.Kind.TP2_LEAD_FOLLOWUP}


def test_a_request_queues_what_a_website_booking_queues():
    """The website path (`create_lead_from_booking`) emits no lead-created event, so a
    portal request queues nothing either."""
    lead = requests.request_trip(_account(), _cleaned())
    assert not TouchPoint.objects.filter(lead=lead).exists()


# --- recent addresses -------------------------------------------------------------------


def test_recent_addresses_are_mine_only_and_distinct(make_trip):
    account = _account()
    iad = Airport.objects.get(iata="IAD")
    older = make_trip(account, pickup_date=_day(-30), stops=[])
    StopFactory(reservation=older, sequence=0, address="1100 Pennsylvania Ave NW")
    StopFactory(reservation=older, sequence=1, address="Dulles (IAD)", airport=iad)
    newer = make_trip(account, pickup_date=_day(-2), stops=[])
    StopFactory(
        reservation=newer,
        sequence=0,
        address="2200 Clarendon Blvd",
        latitude=38.8869,
        longitude=-77.0947,
    )
    StopFactory(reservation=newer, sequence=1, address="1100 pennsylvania ave nw ")
    theirs = ReservationFactory(lead=LeadFactory())
    StopFactory(reservation=theirs, sequence=0, address="Somebody Else's House")

    recent = requests.recent_addresses(account)

    assert [r["address"] for r in recent] == [
        "2200 Clarendon Blvd",
        "1100 pennsylvania ave nw",
        "Dulles (IAD)",
    ]
    assert recent[0]["lat"] == pytest.approx(38.8869)
    assert recent[0]["lng"] == pytest.approx(-77.0947)
    assert recent[2]["airport_id"] == iad.pk and recent[0]["airport_id"] is None
    assert len(requests.recent_addresses(account, limit=2)) == 2


def test_recent_addresses_are_the_ones_used_most_recently_not_the_furthest_ahead(make_trip):
    """A trip booked months ago for next spring must not push last week's address down."""
    account = _account()
    long_ago = make_trip(account, pickup_date=_day(120), stops=[])
    StopFactory(reservation=long_ago, sequence=0, address="Booked long ago for next spring")
    Reservation.objects.filter(pk=long_ago.pk).update(
        created_at=timezone.now() - timedelta(days=90)
    )
    last_week = make_trip(account, pickup_date=_day(-7), stops=[])
    StopFactory(reservation=last_week, sequence=0, address="Used last week")
    Reservation.objects.filter(pk=last_week.pk).update(
        created_at=timezone.now() - timedelta(days=8)
    )

    assert [r["address"] for r in requests.recent_addresses(account)] == [
        "Used last week",
        "Booked long ago for next spring",
    ]


def test_trips_made_in_one_go_fall_back_to_the_latest_pickup_first(make_trip):
    account = _account()
    stamp = timezone.now() - timedelta(days=1)
    for days, address in ((-60, "Older ride"), (-5, "Newer ride")):
        trip = make_trip(account, pickup_date=_day(days), stops=[])
        StopFactory(reservation=trip, sequence=0, address=address)
        Reservation.objects.filter(pk=trip.pk).update(created_at=stamp)

    assert [r["address"] for r in requests.recent_addresses(account)] == [
        "Newer ride",
        "Older ride",
    ]


def test_a_recent_chip_empties_the_pickups_flight_before_filling_the_address(client_as, make_trip):
    """The chip's handler is in the page. A flight typed for one airport must not ride along
    to the address a chip puts in its place, so it clears the airline and flight number
    first, as typing a new address does."""
    account = _account()
    past = make_trip(account, pickup_date=_day(-5), stops=[])
    StopFactory(reservation=past, sequence=0, address="Reagan National (DCA)")
    html = client_as(account).get(URL).content.decode()
    handler = html[html.index("recent(r) {") : html.index('class="card quote-form')]
    clears_flight = handler.index("input[name=pickup_flight]")
    clears_airline = handler.index("select[name=pickup_airline]")
    fills = handler.index("Object.assign(")
    assert clears_flight < fills and clears_airline < fills
    assert "flight.value = ''" in handler and "tomselect.clear(true)" in handler


# --- the form ---------------------------------------------------------------------------


def test_past_date_is_a_form_error(client_as):
    response = client_as(_account()).post(
        URL, _post(pickup_date=_day(-1).isoformat(), notes="Keep this")
    )

    assert response.status_code == 200
    assert "pickup_date" in response.context["form"].errors
    assert "Keep this" in response.content.decode()  # the customer's input is kept
    assert Lead.objects.count() == 0


def test_later_today_is_not_in_the_past():
    noon = datetime(2026, 12, 3, 17, 0, tzinfo=UTC)  # noon in Arlington
    with patch(NOW, return_value=noon):
        assert TripRequestForm(_post(pickup_date="2026-12-03", pickup_time="18:30")).is_valid()


def test_a_time_earlier_today_is_a_form_error():
    noon = datetime(2026, 12, 3, 17, 0, tzinfo=UTC)  # noon in Arlington
    with patch(NOW, return_value=noon):
        form = TripRequestForm(_post(pickup_date="2026-12-03", pickup_time="06:00"))
        assert not form.is_valid()
    assert form.errors == {"pickup_time": ["Pick a time later today, or a later date."]}


def test_the_past_is_judged_on_the_clock_where_the_pickup_is():
    """6:30 AM UTC on Dec 4 is 1:30 AM on Dec 4 in Virginia, and still 10:30 PM on Dec 3
    in Los Angeles: an 11 PM pickup at LAX "on Dec 3" is half an hour away, not yesterday,
    while the same date and time in Arlington is gone."""
    lax = Airport.objects.get(iata="LAX")
    now = datetime(2026, 12, 4, 6, 30, tzinfo=UTC)
    los_angeles = {
        "pickup": "Los Angeles International Airport (LAX)",
        "pickup_lat": "33.9416",
        "pickup_lng": "-118.4085",
        "pickup_airport": str(lax.pk),
    }
    with patch(NOW, return_value=now):
        at_lax = TripRequestForm(
            _post(pickup_date="2026-12-03", pickup_time="23:00", **los_angeles)
        )
        assert at_lax.is_valid(), at_lax.errors
        gone_at_lax = TripRequestForm(
            _post(pickup_date="2026-12-03", pickup_time="22:00", **los_angeles)
        )
        assert set(gone_at_lax.errors) == {"pickup_time"}
        in_arlington = TripRequestForm(_post(pickup_date="2026-12-03", pickup_time="23:00"))
        assert set(in_arlington.errors) == {"pickup_date"}


def test_coordinates_alone_place_the_pickup_and_none_falls_back_to_the_office_zone():
    now = datetime(2026, 12, 4, 6, 30, tzinfo=UTC)
    santa_monica = {
        "pickup": "Santa Monica Pier",
        "pickup_lat": "34.0100",
        "pickup_lng": "-118.4960",
    }
    typed_only = {"pickup": "Some address typed by hand", "pickup_lat": "", "pickup_lng": ""}
    with patch(NOW, return_value=now):
        assert TripRequestForm(
            _post(pickup_date="2026-12-03", pickup_time="23:00", **santa_monica)
        ).is_valid()
        unplaced = TripRequestForm(
            _post(pickup_date="2026-12-03", pickup_time="23:00", **typed_only)
        )
        assert set(unplaced.errors) == {"pickup_date"}


def test_date_time_and_pickup_are_required():
    form = TripRequestForm(_post(pickup_date="", pickup_time="", pickup=""))
    assert not form.is_valid()
    assert {"pickup_date", "pickup_time", "pickup"} <= set(form.errors)


def test_hourly_needs_hours_and_passengers_at_least_one():
    form = TripRequestForm(_post(trip_type="hourly", passengers="0"))
    assert not form.is_valid()
    assert {"hours", "passengers"} <= set(form.errors)


def test_wedding_occasion_cannot_be_submitted():
    wedding = wedding_service_type()
    assert wedding.name == WEDDING_SERVICE_NAME
    form = TripRequestForm(_post(service_type=str(wedding.pk)))
    assert not form.is_valid() and "service_type" in form.errors
    assert str(wedding.pk) not in {value for value, _ in form.occasion_options()}


def test_a_request_is_in_my_trips_and_its_page_opens(client_as):
    account = _account()
    airport = ServiceTypeFactory(name="Airport Transfer")
    client = client_as(account)
    response = client.post(URL, _post(service_type=str(airport.pk)), follow=True)

    trip = Reservation.objects.get()
    assert visible_trips(account).filter(pk=trip.pk).exists()
    assert response.status_code == 200
    assert response.redirect_chain[-1][0] == reverse("customers:trip", args=[trip.pk])
    html = response.content.decode()
    assert "Request received" in html
    assert "Your quote will arrive by email and text." in html  # the toast


def test_the_page_shows_the_form_recent_and_who_is_requesting(client_as, make_trip, settings):
    settings.COMPANY_PHONE = "(703) 555-0142"
    account = _account()
    past = make_trip(account, pickup_date=_day(-5), stops=[])
    StopFactory(reservation=past, sequence=0, address="Reagan National (DCA)")
    VehicleTypeFactory(name="Sprinter")

    response = client_as(account).get(URL)

    html = response.content.decode()
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert "Request a trip" in html and "Send request" in html
    # The chip hands its address, coordinates and airport to the page as a JSON object.
    assert "recent({&quot;address&quot;: &quot;Reagan National (DCA)&quot;, &quot;lat&quot;" in html
    assert "Dana Whitfield" in html and account.contact.email in html
    assert "Travelling soon? Call dispatch at" in html and 'href="tel:+17035550142"' in html
    assert f'href="{reverse("customers:account")}"' in html and "Edit in Account" in html
    assert "Sprinter" in html
    assert "<select" in html and html.count("<select") == html.count("data-tom")
    assert 'name="name"' not in html and 'name="email"' not in html


def test_signed_out_goes_to_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302
    assert response.url.startswith(reverse("customers:login"))


def test_the_shell_now_shows_the_request_a_trip_button(client_as):
    html = client_as(_account()).get(reverse("customers:home")).content.decode()
    assert f'href="{URL}"' in html and "Request a trip" in html


def test_public_booking_form_still_requires_name_and_email():
    form = BookingRequestForm({"passengers": 1})
    assert not form.is_valid()
    assert {"name", "email"} <= set(form.errors)
    assert "company" in BookingRequestForm.base_fields  # the honeypot is still there


# --- fix round 1 ------------------------------------------------------------------------


def _affiliate():
    company = CompanyFactory(is_affiliate_account=True)
    return CustomerAccountFactory(contact=ContactFactory(company=company))


def test_an_affiliate_customer_sees_a_message_not_the_form(client_as, settings):
    settings.COMPANY_PHONE = "(703) 555-0142"
    response = client_as(_affiliate()).get(URL)

    html = response.content.decode()
    assert response.status_code == 200
    assert "(703) 555-0142" in html
    assert "Send request" not in html and 'name="pickup"' not in html


def test_an_affiliate_customer_cannot_post_a_request(client_as):
    account = _affiliate()
    response = client_as(account).post(URL, _post())

    assert response.status_code == 200
    assert Lead.objects.count() == 0
    with pytest.raises(CustomerError):
        requests.request_trip(account, _cleaned())
    assert Lead.objects.count() == 0


def test_the_request_button_is_hidden_for_an_affiliate_customer(client_as):
    nav = f'href="{URL}" class="btn-gold'
    assert nav not in client_as(_affiliate()).get(reverse("customers:home")).content.decode()
    assert nav in client_as(_account()).get(reverse("customers:home")).content.decode()


def test_an_error_rerender_keeps_stops_coordinates_flight_and_rider(client_as):
    iad = Airport.objects.get(iata="IAD")
    united = Airline.objects.get(iata="UA")
    payload = _post(
        pickup_date=_day(-1).isoformat(),
        pickup="Washington Dulles International Airport (IAD)",
        pickup_lat="38.9531",
        pickup_lng="-77.4565",
        pickup_airport=str(iad.pk),
        pickup_airline=str(united.pk),
        pickup_flight="915",
        stops_json=(
            '[{"address": "Reston Town Center", "lat": "38.9586", "lng": "-77.3570",'
            ' "display": "Reston Town Center", "airport": null, "airline": null,'
            ' "flight": "", "direction": ""}]'
        ),
        rider="other",
        rider_name="Sam Exec",
        rider_mobile="+12025550142",
    )
    client = client_as(_account())
    response = client.post(URL, payload)

    html = response.content.decode()
    assert response.status_code == 200 and Lead.objects.count() == 0
    assert "&quot;address&quot;: &quot;Reston Town Center&quot;" in html  # the extra stop
    assert "lat: &quot;38.9531&quot;, lng: &quot;-77.4565&quot;" in html  # pickup coordinates
    assert "lat: &quot;38.8941&quot;, lng: &quot;-77.0280&quot;" in html  # drop-off coordinates
    assert f'<option value="{united.pk}" selected>' in html
    assert 'name="pickup_flight"' in html and 'value="915"' in html
    assert 'value="other" x-model="rider" checked' in html
    assert 'value="Sam Exec"' in html

    payload["pickup_date"] = _day().isoformat()
    client.post(URL, payload)
    trip = Lead.objects.get().reservations.get()
    assert [s.address for s in trip.stops.order_by("sequence")] == [
        "Washington Dulles International Airport (IAD)",
        "Reston Town Center",
        "1100 Pennsylvania Ave NW, Washington, DC",
    ]
    assert trip.pickup_timezone == "America/New_York"


def test_the_public_widget_posts_no_coordinates_it_was_not_given(client):
    html = client.get("/bookings/").content.decode()
    assert 'x-data="addressAutocomplete(' in html
    assert "lat: &quot;&quot;, lng: &quot;&quot;" in html


def test_a_long_contact_name_is_cut_to_the_passenger_name_length():
    account = _account(name="N" * 150)
    lead = requests.request_trip(account, _cleaned())

    limit = Passenger._meta.get_field("name").max_length
    assert lead.reservations.get().named_passengers.get().name == "N" * limit


def test_the_flight_direction_buttons_can_be_reached_and_read(client_as):
    html = client_as(_account()).get(URL).content.decode()
    for label in ("Arriving", "Departing"):
        button = html[html.rindex("<button", 0, html.index(f">{label}</button>")) :]
        button = button[: button.index(">") + 1]
        assert "focus-gold" in button and ":aria-pressed=" in button
        assert "hover:bg-surface2" in button


def test_field_limits_come_from_the_form_not_the_template(client_as):
    html = client_as(_account()).get(URL).content.decode()
    name_max = Passenger._meta.get_field("name").max_length
    reference_max = Reservation._meta.get_field("customer_reference").max_length
    assert f'name="rider_name" value="" maxlength="{name_max}"' in html
    assert f'name="customer_reference" value="" maxlength="{reference_max}"' in html
