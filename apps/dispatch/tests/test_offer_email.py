from datetime import date, time
from decimal import Decimal

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
