"""Template registry completeness + context building from real lead data."""

from datetime import date, time, timedelta

import pytest

from apps.leads.factories import LeadFactory
from apps.messaging import touchpoint_templates as tpl
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory, StopFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def test_registry_covers_every_kind():
    assert set(tpl.TEMPLATES) == set(TouchPoint.Kind.values)


def test_offsets_and_channels_match_client_cadence():
    t = tpl.TEMPLATES
    assert t["tp1_welcome"].offset == timedelta(minutes=30)
    assert t["tp1_welcome"].channels == ("email", "sms")
    assert t["tp2_lead_followup"].offset == timedelta(hours=2)
    assert t["tp3_quote_sent_sms"].offset == timedelta(minutes=3)
    assert t["tp4_viewed_sms"].offset == timedelta(minutes=20)
    assert t["tp5_viewed_email"].offset == timedelta(hours=2)
    assert t["tp6_quote_followup"].offset == timedelta(hours=24)
    assert t["tp7_expiring"].offset == timedelta(hours=-24)
    assert t["tp7_expiring"].anchor == "quote_expires"
    assert t["tp8_expired"].offset == timedelta(hours=24)


def test_every_template_renders_with_context(settings):
    settings.COMPANY_PHONE = "(202) 424-2600"
    lead = LeadFactory(contact__name="Jane Doe")
    res = ReservationFactory(lead=lead)
    StopFactory(reservation=res, sequence=0, address="JFK Airport")
    ctx = tpl.build_context(lead)
    ctx["quote_link"] = "https://x/quote/tok/"
    ctx["review_link"] = "https://x/rev"
    for template in tpl.TEMPLATES.values():
        for text in (template.subject, template.email_body, template.sms_body):
            tpl.render(text, ctx)  # KeyError => missing context key


def test_context_values():
    lead = LeadFactory(contact__name="Jane Doe")
    ctx = tpl.build_context(lead)
    assert ctx["first_name"] == "Jane"
    assert ctx["quote_no"] == lead.quote_no


CANCELLED = Reservation.TripStatus.CANCELLED


def test_order_level_values_skip_a_cancelled_first_trip():
    """PROC-13: the thank-you named the cancelled first trip's date, not the trip that ran."""
    lead = LeadFactory()
    ReservationFactory(
        lead=lead,
        sort_order=0,
        pickup_date=date(2026, 11, 3),
        pickup_time=time(7, 0),
        trip_status=CANCELLED,
        stops=["Cancelled pickup", "Cancelled drop-off"],
    )
    ReservationFactory(
        lead=lead,
        sort_order=1,
        pickup_date=date(2026, 11, 20),
        pickup_time=time(9, 30),
        stops=["Live pickup", "Live drop-off"],
    )
    ctx = tpl.build_context(lead)
    assert ctx["pickup_date"] == "Nov 20, 2026"
    assert ctx["pickup_time"] == "9:30 AM"
    assert ctx["pickup_address"] == "Live pickup"
    assert ctx["dropoff_address"] == "Live drop-off"
    rendered = tpl.render(tpl.TEMPLATES["thank_you"].email_body, ctx)
    assert "booking with us for Nov 20, 2026." in rendered


def test_order_level_values_fall_back_to_the_first_trip_when_all_are_cancelled():
    """A whole-order cancel or release still names the booking it's about."""
    lead = LeadFactory()
    ReservationFactory(
        lead=lead,
        sort_order=0,
        pickup_date=date(2026, 11, 3),
        trip_status=CANCELLED,
        stops=["First pickup"],
    )
    ReservationFactory(
        lead=lead,
        sort_order=1,
        pickup_date=date(2026, 11, 20),
        trip_status=CANCELLED,
    )
    ctx = tpl.build_context(lead)
    assert ctx["pickup_date"] == "Nov 3, 2026"
    assert ctx["pickup_address"] == "First pickup"


def test_order_level_values_use_the_earliest_pickup_not_the_first_line():
    """PROC-13 (Moe, 2026-11-10): the order's trip is the earliest live pickup, so reordering
    the quote's lines can't change which trip a message names."""
    lead = LeadFactory()
    ReservationFactory(
        lead=lead,
        sort_order=0,
        pickup_date=date(2026, 12, 20),
        pickup_time=time(8, 0),
        stops=["Later pickup"],
    )
    ReservationFactory(
        lead=lead,
        sort_order=1,
        pickup_date=date(2026, 11, 21),
        pickup_time=time(10, 0),
        stops=["Earlier pickup"],
    )
    ReservationFactory(lead=lead, sort_order=2, pickup_date=None, stops=["Undated"])
    ctx = tpl.build_context(lead)
    assert ctx["pickup_date"] == "Nov 21, 2026"
    assert ctx["pickup_address"] == "Earlier pickup"


def test_earliest_pickup_compares_instants_across_zones():
    """10 AM in New York comes before 9 AM in Los Angeles on the same day."""
    lead = LeadFactory()
    ReservationFactory(
        lead=lead,
        sort_order=0,
        pickup_date=date(2026, 11, 21),
        pickup_time=time(9, 0),
        pickup_timezone="America/Los_Angeles",
        stops=["LAX"],
    )
    ReservationFactory(
        lead=lead,
        sort_order=1,
        pickup_date=date(2026, 11, 21),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
        stops=["DCA"],
    )
    assert tpl.build_context(lead)["pickup_address"] == "DCA"


def test_all_cancelled_falls_back_to_the_earliest_pickup():
    lead = LeadFactory()
    ReservationFactory(
        lead=lead, sort_order=0, pickup_date=date(2026, 12, 1), trip_status=CANCELLED
    )
    ReservationFactory(
        lead=lead, sort_order=1, pickup_date=date(2026, 11, 5), trip_status=CANCELLED
    )
    assert tpl.build_context(lead)["pickup_date"] == "Nov 5, 2026"


def test_message_dates_drop_the_leading_zero():
    """PROC-13: "Nov 08" read as a typo in customer copy."""
    lead = LeadFactory()
    res = ReservationFactory(
        lead=lead,
        pickup_date=date(2026, 11, 8),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
    )
    ctx = tpl.build_context(lead, res)
    assert ctx["pickup_date"] == "Nov 8, 2026"
    assert ctx["trip_pickup_date"] == "Nov 8, 2026"
    assert ctx["trip_pickup_when"].startswith("Nov 8, 2026 at 10:00 AM")
    assert ctx["quote_breakdown"].startswith("Nov 8 · 10:00 AM")


def test_phone_numbers_in_messages_read_as_people_write_them(settings):
    """MSG-03/05/09: the driver's cell, the number we'll reach the customer at, the driver
    list and the company phone printed raw E.164 ("+12025550190"). They read
    "(202) 555-0190", as the editor's sample already does."""
    from apps.contacts.factories import ContactFactory
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment
    from apps.messaging import sender
    from apps.messaging.models import MessageTemplate
    from apps.messaging.recipients import Recipient

    settings.COMPANY_PHONE = "+12025550100"
    lead = LeadFactory(contact=ContactFactory(name="Mia Messages", phone="+12025550142"))
    trip = ReservationFactory(
        lead=lead,
        pickup_date=date(2027, 3, 12),
        pickup_time=time(9, 30),
        pickup_timezone="America/New_York",
        trip_status=Reservation.TripStatus.DISPATCHED,
    )
    AssignmentFactory(
        reservation=trip,
        status=Assignment.Status.CONFIRMED,
        driver_name="Sam Rivera",
        driver_cell="+12025550190",
        vehicle_desc="Suburban",
        vehicle_number="9",
    )

    ctx = tpl.build_context(lead, trip)

    assert ctx["driver_cell"] == "(202) 555-0190"
    assert ctx["contact_on_file"] == "Mia Messages · (202) 555-0142"
    assert ctx["company_phone"] == "(202) 555-0100"
    assert "Driver: Sam Rivera · (202) 555-0190" in ctx["trip_sheet_drivers"]

    row = MessageTemplate.objects.get(key="trip_confirm_customer")
    tp = TouchPoint(lead=lead, reservation=trip, message=row, kind=row.key)
    person = Recipient(
        name="Mia Messages",
        label="Mia Messages (booking contact)",
        phone="+12025550142",
        email="mia@example.com",
        contact_id=lead.contact_id,
        channels=frozenset({"sms", "email"}),
    )
    assert sender.context(tp, row, person)["contact_on_file"] == "Mia Messages · (202) 555-0142"
