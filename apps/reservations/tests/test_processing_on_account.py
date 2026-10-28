"""APC-105: a trip of an order billed to an account is never a card action (spec §4.4,
"Invoiced trip"). Processing locks it with its price still owed: Customer Billing (APC-106)
puts it on the next invoice.

Stripe and Podium are patched to fail loudly: nothing here may reach either.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import trip_money
from apps.payments.factories import PaymentPlanFactory
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import ProcessingEvent, Reservation, TripExtra

pytestmark = pytest.mark.django_db
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
        patch("apps.payments.services._stripe", side_effect=AssertionError("Stripe called")),
    ):
        yield


@pytest.fixture
def agent():
    return UserFactory(can_manage_payments=True)


def _trip(*, account=True, contact=None):
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=contact or ContactFactory())
    # A card on file, which a card order would charge: the account must win.
    PaymentPlanFactory(lead=lead, stripe_payment_method_id="pm_1", card_last4="4242")
    if account:
        lead.billing_group = AccountGroupFactory(
            account__contact=lead.contact, account__name="Ridgeline", name="Accounts Payable"
        )
        lead.po_number = "4471"
        lead.save(update_fields=["billing_group", "po_number"])
    trip = ReservationFactory(
        lead=lead,
        trip_status=S.DONE,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _diff(trip):
    return trip_money.money_for(Reservation.objects.get(pk=trip.pk)).difference


def test_the_list_says_invoice_for_an_account_customer():
    trip = _trip()
    [group] = processing.group_by_customer(
        processing.ready_trips(processing.Filters.from_query({}))
    )
    assert group.contact == trip.lead.contact
    assert group.pays == "Invoice"


def test_a_customer_with_card_and_account_orders_shows_both():
    trip = _trip()
    _trip(account=False, contact=trip.lead.contact)
    [group] = processing.group_by_customer(
        processing.ready_trips(processing.Filters.from_query({}))
    )
    assert group.pays == "Card ••4242 · Invoice"


def test_processing_for_the_invoice_locks_with_the_price_still_owed(agent):
    trip = _trip()

    review = processing.process(trip, user=agent, expected="600.00", collect="invoice")

    assert review.is_processed
    assert _diff(trip) == Decimal("600.00")
    assert not trip.lead.payment.charges.exists()
    event = review.events.get(kind=ProcessingEvent.Kind.PROCESSED)
    assert event.note == (
        "Billed to Ridgeline — Accounts Payable · PO 4471. Goes on the next invoice."
    )


@pytest.mark.parametrize("collect", ["card", "link", ""])
def test_card_and_link_are_refused_on_account(agent, collect):
    trip = _trip()
    with pytest.raises(processing.ProcessingError, match="billed to an account"):
        processing.process(trip, user=agent, expected="600.00", collect=collect)
    assert not processing.review_of(trip) or not processing.review_of(trip).is_processed


def test_the_invoice_path_is_refused_on_a_card_order(agent):
    trip = _trip(account=False)
    with pytest.raises(processing.ProcessingError, match="Charge the card"):
        processing.process(trip, user=agent, expected="600.00", collect="invoice")


def test_a_charge_added_later_goes_on_the_invoice(agent):
    trip = _trip()
    processing.process(trip, user=agent, expected="600.00", collect="invoice")

    processing.add_charge(trip, user=agent, amount="50.00", label="Cleaning", collect="invoice")

    assert TripExtra.objects.filter(reservation=trip, label="Cleaning").exists()
    assert _diff(trip) == Decimal("650.00")
    assert not trip.lead.payment.charges.exists()


@pytest.mark.parametrize("collect", ["card", "link"])
def test_a_charge_added_later_never_hits_the_card(agent, collect):
    trip = _trip()
    processing.process(trip, user=agent, expected="600.00", collect="invoice")

    with pytest.raises(processing.ProcessingError, match="billed to an account"):
        processing.add_charge(trip, user=agent, amount="50.00", label="Cleaning", collect=collect)
    assert not TripExtra.objects.filter(reservation=trip).exists()


def test_the_trip_money_doors_refuse_too():
    trip = _trip()
    with pytest.raises(Exception, match="billed to an account"):
        trip_money.charge_trip(trip, Decimal("10.00"))
    with pytest.raises(Exception, match="billed to an account"):
        trip_money.open_trip_intent(trip, Decimal("10.00"))


def test_the_pane_offers_one_process_button_for_the_invoice(client, agent):
    trip = _trip()
    client.force_login(agent)

    html = client.get(reverse("processing"), {"trip": trip.pk}).content.decode()

    assert "Billed to Ridgeline — Accounts Payable · PO 4471. Goes on the next invoice." in html
    assert "processInvoice()" in html
    assert "Process and charge" not in html
    assert "Process and send pay link" not in html
