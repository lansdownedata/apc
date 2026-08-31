"""Task 6: public quote page — view tracking, expiry states, T&Cs, book-now checkout."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import ReservationFactory, TransferReservationFactory
from apps.reservations.models import Stop

pytestmark = pytest.mark.django_db


def _quoted_lead(**kwargs):
    kwargs.setdefault("status", Lead.Status.QUOTED)
    kwargs.setdefault("contact", ContactFactory(email="rider@example.com"))
    kwargs.setdefault("quote_expires_at", timezone.now() + timezone.timedelta(days=10))
    lead = LeadFactory(**kwargs)
    TransferReservationFactory(
        lead=lead,
        rate=Decimal("185.00"),
        service_type=ServiceTypeFactory(name="Airport Transfer"),
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("185.00"), deposit_pct=50)
    return lead


def test_quote_page_rejects_bad_token(client):
    resp = client.get(reverse("quote_page", args=["not-a-real-token"]))
    assert resp.status_code == 404


def test_quote_page_renders_quote_summary(client):
    lead = _quoted_lead()
    reservation = lead.reservations.first()
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert lead.quote_no in body
    assert reservation.service_type.name in body
    assert "50% deposit" in body
    assert "92.50" in body  # deposit amount
    assert "Terms &amp; Conditions" in body or "Terms & Conditions" in body


def test_quote_page_new_status_404s(client):
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email="a@b.com"))
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 404


def test_quote_page_lost_status_404s(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.LOST
    lead.save(update_fields=["status"])
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 404


def test_quote_page_first_view_stamps_and_schedules(client):
    lead = _quoted_lead()
    assert lead.quote_viewed_at is None
    token = services.make_deposit_token(lead)
    with patch.object(services.touchpoints, "schedule_quote_viewed") as scheduled:
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    assert lead.quote_viewed_at is not None
    scheduled.assert_called_once_with(lead)


def test_quote_page_second_view_does_not_restamp(client):
    lead = _quoted_lead()
    token = services.make_deposit_token(lead)
    with patch.object(services.touchpoints, "schedule_quote_viewed"):
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    first_stamp = lead.quote_viewed_at

    with patch.object(services.touchpoints, "schedule_quote_viewed") as scheduled:
        client.get(reverse("quote_page", args=[token]))
    lead.refresh_from_db()
    assert lead.quote_viewed_at == first_stamp
    scheduled.assert_not_called()


def test_quote_page_expired_has_no_book_button_and_notifies_once(client):
    lead = _quoted_lead(quote_expires_at=timezone.now() - timezone.timedelta(days=1))
    token = services.make_deposit_token(lead)

    resp1 = client.get(reverse("quote_page", args=[token]))
    assert "expired" in resp1.content.decode().lower()
    assert "Book Now" not in resp1.content.decode()

    resp2 = client.get(reverse("quote_page", args=[token]))
    assert "expired" in resp2.content.decode().lower()

    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.QUOTE_EXPIRED).count() == 1


def test_quote_page_booked_paid_shows_already_booked_note(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    plan = lead.payment
    plan.deposit_status = plan.DepositStatus.PAID
    plan.save(update_fields=["deposit_status", "updated_at"])
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 200
    assert "already booked" in resp.content.decode().lower()
    assert "Book Now" not in resp.content.decode()


def test_quote_page_booked_unpaid_shows_book_now(client):
    lead = _quoted_lead()
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_page", args=[token]))
    assert resp.status_code == 200
    assert "Book Now" in resp.content.decode()
    assert "already booked" not in resp.content.decode().lower()


# --- Per-vehicle itinerary cards (Task 6) ---
# NOTE: "Signature SUV" is used instead of "Luxury SUV" deliberately — migration
# 0003_seed_vehicles pre-seeds "Luxury SUV" (among 5 others), and VehicleTypeFactory's
# django_get_or_create=("name",) means a factory call with that name FETCHES the seeded
# row (capacity 6, blank description) instead of creating one with the description this
# test asserts on. See CLAUDE.local.md gotchas / task brief hazard notes.


@pytest.fixture
def lead_with_itinerary():
    lead = LeadFactory(
        status=Lead.Status.QUOTED,
        passenger_names="Shane Thomas",
        billing_contact=ContactFactory(name="Dana Ledger"),
    )
    vt = VehicleTypeFactory(name="Signature SUV", capacity=6, description="Leather, chilled water.")
    res = ReservationFactory(lead=lead, vehicle=vt, rate="594.71", stops=[])
    Stop.objects.create(
        reservation=res,
        sequence=0,
        name="SpringHill Suites by Marriott Frederick",
        address="111 Byte Drive, Frederick, MD 21702",
    )
    Stop.objects.create(
        reservation=res,
        sequence=1,
        name="Ceresville Mansion",
        address="8529 Liberty Road, Frederick, MD 21701",
    )
    return lead


def _get_quote(client, lead):
    return client.get(reverse("quote_page", args=[services.make_deposit_token(lead)]))


def test_renders_venue_names_and_addresses(client, lead_with_itinerary):
    content = _get_quote(client, lead_with_itinerary).content.decode()
    assert "SpringHill Suites by Marriott Frederick" in content
    assert "111 Byte Drive" in content
    assert "Ceresville Mansion" in content


def test_renders_the_vehicle_type(client, lead_with_itinerary):
    content = _get_quote(client, lead_with_itinerary).content.decode()
    assert "Signature SUV" in content
    assert "Leather, chilled water." in content


def test_renders_passengers_and_billing_contact(client, lead_with_itinerary):
    content = _get_quote(client, lead_with_itinerary).content.decode()
    assert "Shane Thomas" in content
    assert "Dana Ledger" in content  # billing contact
    assert lead_with_itinerary.contact.name in content  # booking contact


def test_survives_a_reservation_with_no_vehicle(client):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    res = ReservationFactory(lead=lead, vehicle=None, rate="200.00")
    Stop.objects.create(reservation=res, sequence=0, address="A St")
    Stop.objects.create(reservation=res, sequence=1, address="B St")
    assert _get_quote(client, lead).status_code == 200


def test_stops_render_in_sequence(client, lead_with_itinerary):
    content = _get_quote(client, lead_with_itinerary).content.decode()
    assert content.index("SpringHill") < content.index("Ceresville")


def test_blank_vehicle_image_shows_placeholder_not_broken_img(client, lead_with_itinerary):
    """VehicleTypeFactory's default image is blank — must render the placeholder div,
    never an <img> tag pointing at an empty/None file. (The page logo is also an <img>,
    so assert on the vehicle-specific alt text rather than the tag's mere presence.)"""
    content = _get_quote(client, lead_with_itinerary).content.decode()
    assert "No photo" in content
    assert 'alt="Signature SUV"' not in content


def test_vehicle_with_image_renders_img_tag(client, tmp_path, settings):
    """A VehicleType with a real uploaded image renders an <img> tag (not the placeholder)."""
    settings.MEDIA_ROOT = tmp_path

    from io import BytesIO

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (10, 10), "gold").save(buf, format="PNG")
    image_file = SimpleUploadedFile("vehicle.png", buf.getvalue(), content_type="image/png")

    lead = LeadFactory(status=Lead.Status.QUOTED)
    vt = VehicleTypeFactory(name="Panorama Coach", image=image_file)
    ReservationFactory(lead=lead, vehicle=vt, rate="200.00")

    content = _get_quote(client, lead).content.decode()
    assert 'alt="Panorama Coach"' in content
    assert "No photo" not in content


# --- the quote page reads arriving/departing flights off the route ---


def _with_flight(reservation, *, sequence=0, number="123"):
    """Attach IAD + United + `number` to the stop at `sequence` and return it."""
    from apps.addresses.models import Airline, Airport

    stop = reservation.stops.get(sequence=sequence)
    stop.airport = Airport.objects.get(iata="IAD")  # seeded by addresses.0003
    stop.airline = Airline.objects.get(iata="UA")
    stop.flight_number = number
    stop.save()
    return stop


def test_quote_page_reads_arriving_and_departing_flights(client):
    lead = _quoted_lead()
    reservation = lead.reservations.first()
    _with_flight(reservation, sequence=0, number="123")
    _with_flight(reservation, sequence=1, number="456")
    token = services.make_deposit_token(lead)
    html = client.get(reverse("quote_page", args=[token])).content.decode()
    assert "Arriving on United Airlines 123" in html
    assert "Departing on United Airlines 456" in html


def test_quote_page_renders_a_private_tail_number_alone(client):
    """`flight_label_long` would otherwise read "Private / Tail number N561FX" — for the
    seeded Private carrier, just the tail number is customer-facing-correct (2026-08-29 §4)."""
    from apps.addresses.models import Airline, Airport

    lead = _quoted_lead()
    reservation = lead.reservations.first()
    stop = reservation.stops.get(sequence=0)
    stop.airport = Airport.objects.get(iata="IAD")
    stop.airline = Airline.objects.get(iata="N")
    stop.flight_number = "N561FX"
    stop.save()
    token = services.make_deposit_token(lead)
    html = client.get(reverse("quote_page", args=[token])).content.decode()
    assert "Arriving on N561FX" in html
    assert "Private" not in html
