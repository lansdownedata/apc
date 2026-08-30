from datetime import UTC, date, time
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.dispatch import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _trip():
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 8, 26),
        pickup_time=time(6, 15),
        rate=285,
        hours=1,
    )


def test_offer_emails_the_vendor_with_the_payout(mailoutbox):
    vendor = VendorFactory(name="Capital Chauffeurs", email="ops@capital.example")
    services.send_offer(_trip(), vendor, payout=Decimal("215.00"))
    assert len(mailoutbox) == 1
    message = mailoutbox[0]
    assert message.to == ["ops@capital.example"]
    assert "6:15 a.m." in message.body or "6:15 AM" in message.body
    assert "215.00" in message.body


def test_offer_email_never_shows_the_customer_price(mailoutbox):
    vendor = VendorFactory(email="ops@capital.example")
    services.send_offer(_trip(), vendor, payout=Decimal("215.00"))  # customer total is 285.00
    body = mailoutbox[0].body + mailoutbox[0].alternatives[0][0]
    assert "285" not in body


def test_offer_email_formats_the_payout_as_money(mailoutbox):
    """A bare 1200.00 is the kind of figure an affiliate misreads at a glance."""
    services.send_offer(_trip(), VendorFactory(email="ops@x.example"), payout=Decimal("1200.00"))
    body = mailoutbox[0].body + mailoutbox[0].alternatives[0][0]
    assert "$1,200.00" in body


def test_a_vendor_without_an_email_still_gets_the_assignment(mailoutbox):
    vendor = VendorFactory(email="")
    assignment = services.send_offer(_trip(), vendor, payout=Decimal("215.00"))
    assert assignment.pk is not None
    assert len(mailoutbox) == 0


def test_direct_assign_does_not_email(mailoutbox):
    services.assign_direct(_trip(), VendorFactory(email="x@y.example"), payout=Decimal("215.00"))
    assert len(mailoutbox) == 0


def test_a_failed_send_alerts_but_keeps_the_offer(monkeypatch):
    monkeypatch.setattr("apps.dispatch.services.send_html_email", lambda **kw: False)
    trip = _trip()
    assignment = services.send_offer(trip, VendorFactory(email="x@y.example"), payout=Decimal("1"))
    assert assignment.status == services.Assignment.Status.OFFERED
    assert Notification.objects.filter(lead=trip.lead).exists()


def test_offer_email_carries_the_company_branding(mailoutbox, settings):
    vendor = VendorFactory(email="ops@capital.example")
    services.send_offer(_trip(), vendor, payout=Decimal("215.00"))
    html = mailoutbox[0].alternatives[0][0]
    assert settings.COMPANY_NAME in html


def test_offer_email_embeds_the_logo_inline(mailoutbox):
    """The static/brand/apc-logo-email.png asset resolves in this environment (verified via
    finders.find), so the offer email must attach it inline rather than reference a dead
    cid: placeholder — matching how send_quote embeds the same logo."""
    vendor = VendorFactory(email="ops@capital.example")
    services.send_offer(_trip(), vendor, payout=Decimal("215.00"))
    message = mailoutbox[0]
    cids = [a["Content-ID"] for a in message.attachments if hasattr(a, "get")]
    assert "<logo>" in cids


def test_a_gnet_capable_vendor_gets_no_trip_sheet_email(mailoutbox):
    """A GNet-capable vendor is farmed out over the gateway, not by email — even though
    the vendor has an email on file, send_offer must not send anything through it."""
    vendor = VendorFactory(gnet_grid_id="gnet-partner-1", email="ops@capital.example")
    with patch.object(services, "gnet_sync"):
        services.send_offer(_trip(), vendor, payout=Decimal("215.00"))
    assert len(mailoutbox) == 0


# --- the trip sheet lists the flight on an airport stop ---


def _with_flight(reservation, *, sequence=0, number="123"):
    """Attach IAD + United + `number` to the stop at `sequence` and return it."""
    from apps.addresses.models import Airline, Airport

    stop = reservation.stops.get(sequence=sequence)
    stop.airport = Airport.objects.get(iata="IAD")  # seeded by addresses.0003
    stop.airline = Airline.objects.get(iata="UA")
    stop.flight_number = number
    stop.save()
    return stop


def test_offer_email_lists_the_flight_on_an_airport_stop(mailoutbox):
    trip = _trip()
    _with_flight(trip)
    services.send_offer(trip, VendorFactory(email="ops@capital.example"), payout=Decimal("215.00"))
    text, html = mailoutbox[0].body, mailoutbox[0].alternatives[0][0]
    assert "flight UA 123" in text
    assert "✈ UA 123" in html


def test_offer_email_carries_the_verified_time_and_terminal(mailoutbox):
    from datetime import datetime

    from apps.reservations.factories import FlightFactory

    trip = _trip()
    stop = _with_flight(trip)
    stop.flight_direction = "arrival"
    stop.flight = FlightFactory(
        airline=stop.airline,
        airport=stop.airport,
        flight_number="123",
        flight_date=date(2026, 8, 26),
        direction="arrival",
        terminal="B",
        scheduled_at=datetime(2026, 8, 26, 14, 35, tzinfo=UTC),
    )
    stop.save()
    services.send_offer(trip, VendorFactory(email="ops@capital.example"), payout=Decimal("215.00"))
    text, html = mailoutbox[0].body, mailoutbox[0].alternatives[0][0]
    assert "flight UA 123 arr 10:35 AM EDT, Terminal B" in text
    assert "✈ UA 123 · arr 10:35 AM EDT · Terminal B" in html


def test_offer_email_shows_the_departure_direction_word(mailoutbox):
    """Final review #7b: a hardcoded 'arr' in both templates left all other offer-email
    tests (including the arrival case above) passing — nothing exercised a departure stop
    with a flight attached."""
    from datetime import datetime

    from apps.reservations.factories import FlightFactory

    trip = _trip()
    stop = _with_flight(trip)
    stop.flight_direction = "departure"
    stop.flight = FlightFactory(
        airline=stop.airline,
        airport=stop.airport,
        flight_number="123",
        flight_date=date(2026, 8, 26),
        direction="departure",
        terminal="B",
        scheduled_at=datetime(2026, 8, 26, 14, 35, tzinfo=UTC),
    )
    stop.save()
    services.send_offer(trip, VendorFactory(email="ops@capital.example"), payout=Decimal("215.00"))
    text, html = mailoutbox[0].body, mailoutbox[0].alternatives[0][0]
    assert "flight UA 123 dep 10:35 AM EDT, Terminal B" in text
    assert "✈ UA 123 · dep 10:35 AM EDT · Terminal B" in html
