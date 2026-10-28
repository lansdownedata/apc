"""APC-105: an order billed to a billing account group instead of a card (spec 2026-10-25 §5).

The switch is a label on the order, nothing more: no money moves, so the ledger must read
the same before and after, both ways. What it changes is everything that assumes a card,
and those paths are tested where they live.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import selectors, services
from apps.billing.services import BillingError
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.models import JournalEntry
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import TripReview

pytestmark = pytest.mark.django_db


def _account(contact, *, po_required=False, group_name="Accounts Payable"):
    return services.create_account(
        contact,
        name=f"{contact.name} account",
        group_name=group_name,
        invoice_email="ap@example.com",
        po_required=po_required,
    )


def _order(status=Lead.Status.BOOKED, **over):
    lead = LeadFactory(status=status, **over)
    account = _account(lead.contact)
    return lead, account.groups.get()


def _collect(lead, amount):
    ledger.post_capture(
        lead=lead,
        amount=Decimal(amount),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"test-capture-{lead.pk}-{amount}",
    )


# --- bill_to_account -----------------------------------------------------------------


@pytest.mark.parametrize("status", [Lead.Status.NEW, Lead.Status.QUOTED, Lead.Status.BOOKED])
def test_bills_an_order_to_a_group_of_the_customers_account(status):
    lead, group = _order(status=status)

    services.bill_to_account(lead, group, po_number=" 4471 ", user=None)

    lead.refresh_from_db()
    assert lead.billing_group == group
    assert lead.po_number == "4471"
    assert lead.on_account


def test_the_billing_contacts_account_is_offered_too():
    company = CompanyFactory(billing_contact=ContactFactory(name="Pat Payables"))
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(company=company))
    group = _account(company.billing_contact).groups.get()

    services.bill_to_account(lead, group, po_number="", user=None)

    assert Lead.objects.get(pk=lead.pk).billing_group == group


def test_another_customers_group_is_refused():
    lead, _ = _order()
    stranger = _account(ContactFactory()).groups.get()

    with pytest.raises(BillingError, match="isn't one of this customer's"):
        services.bill_to_account(lead, stranger, po_number="", user=None)
    assert Lead.objects.get(pk=lead.pk).billing_group is None


def test_a_group_that_needs_a_po_refuses_a_blank_one_and_names_itself():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    group = _account(lead.contact, po_required=True, group_name="Events").groups.get()

    with pytest.raises(BillingError, match="Events needs a PO number"):
        services.bill_to_account(lead, group, po_number="  ", user=None)


def test_a_lost_order_is_refused():
    lead, group = _order(status=Lead.Status.LOST)
    with pytest.raises(BillingError, match="lost"):
        services.bill_to_account(lead, group, po_number="", user=None)


def test_an_order_with_a_deposit_on_hold_is_refused():
    lead, group = _order(status=Lead.Status.ENGAGED)
    with pytest.raises(BillingError, match="on hold"):
        services.bill_to_account(lead, group, po_number="", user=None)


def test_an_order_with_card_money_collected_is_refused():
    lead, group = _order()
    _collect(lead, "100.00")

    with pytest.raises(BillingError, match="Refund the card payments first"):
        services.bill_to_account(lead, group, po_number="", user=None)


def test_an_order_refunded_back_to_nothing_collected_can_move():
    lead, group = _order()
    _collect(lead, "100.00")
    ledger.post_refund(lead=lead, amount=Decimal("100.00"), idempotency_key="test-refund")

    services.bill_to_account(lead, group, po_number="", user=None)
    assert Lead.objects.get(pk=lead.pk).on_account


def test_the_switch_moves_no_money_either_way():
    lead, group = _order()
    before = (ledger.order_balances(lead), JournalEntry.objects.count())

    services.bill_to_account(lead, group, po_number="", user=None)
    assert (ledger.order_balances(lead), JournalEntry.objects.count()) == before

    services.bill_to_card(Lead.objects.get(pk=lead.pk), user=None)
    assert (ledger.order_balances(lead), JournalEntry.objects.count()) == before


def test_the_label_reads_account_group_and_po():
    lead, group = _order()
    services.bill_to_account(lead, group, po_number="4471", user=None)
    lead = Lead.objects.get(pk=lead.pk)

    assert lead.billed_to_label == f"{group.account.name} — Accounts Payable · PO 4471"


# --- bill_to_card --------------------------------------------------------------------


def test_back_to_card_clears_the_group_and_po():
    lead, group = _order()
    services.bill_to_account(lead, group, po_number="4471", user=None)

    services.bill_to_card(Lead.objects.get(pk=lead.pk), user=None)

    lead = Lead.objects.get(pk=lead.pk)
    assert (lead.billing_group, lead.po_number, lead.on_account) == (None, "", False)


def test_back_to_card_is_refused_once_a_trip_was_processed_on_account():
    lead, group = _order()
    trip = ReservationFactory(lead=lead)
    services.bill_to_account(lead, group, po_number="", user=None)
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    with pytest.raises(BillingError, match="Unlock its processed trips first"):
        services.bill_to_card(Lead.objects.get(pk=lead.pk), user=None)


# --- groups ----------------------------------------------------------------------------


def test_a_group_an_order_bills_to_cannot_be_removed():
    lead, group = _order()
    other = services.add_group(group.account, name="Events")
    services.set_default_group(other)
    services.bill_to_account(lead, group, po_number="", user=None)
    group.refresh_from_db()

    with pytest.raises(BillingError, match="1 order bills to this group"):
        services.delete_group(group)


def test_groups_for_lead_covers_both_contacts_once():
    company = CompanyFactory(billing_contact=ContactFactory())
    booker = ContactFactory(company=company)
    lead = LeadFactory(contact=booker)
    mine = _account(booker).groups.get()
    theirs = _account(company.billing_contact).groups.get()

    assert selectors.groups_for_lead(lead) == [mine, theirs]

    lead.billing_contact = booker  # the same person twice: listed once
    assert selectors.groups_for_lead(lead) == [mine]


def test_groups_for_lead_is_empty_without_an_account():
    assert selectors.groups_for_lead(LeadFactory()) == []


# --- what the card already holds (plan review, APC-105) ----------------------------------


def test_an_order_with_a_processed_trip_cannot_move_to_account():
    lead, group = _order()
    trip = ReservationFactory(lead=lead)
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    with pytest.raises(BillingError, match="processed"):
        services.bill_to_account(lead, group, po_number="", user=None)


def _open_intent(lead, pi="pi_open"):
    from apps.payments.factories import PaymentPlanFactory
    from apps.payments.models import Charge

    plan = PaymentPlanFactory(lead=lead)
    return plan.charges.create(
        kind=Charge.Kind.DEPOSIT,
        amount=Decimal("100.00"),
        status=Charge.Status.PENDING,
        stripe_payment_intent_id=pi,
        idempotency_key=f"k-{pi}",
    )


def test_an_open_card_payment_is_cancelled_at_the_switch():
    from unittest.mock import patch

    from apps.payments.models import Charge

    lead, group = _order()
    charge = _open_intent(lead)

    with patch("apps.payments.services._stripe") as stripe:
        services.bill_to_account(lead, group, po_number="", user=None)

    stripe.return_value.PaymentIntent.cancel.assert_called_once_with("pi_open")
    charge.refresh_from_db()
    assert charge.status == Charge.Status.FAILED


def test_a_card_payment_that_just_went_through_stops_the_switch():
    from unittest.mock import MagicMock, patch

    import stripe as stripe_lib

    lead, group = _order()
    _open_intent(lead)

    with patch("apps.payments.services._stripe") as stripe:
        stripe.return_value.PaymentIntent.cancel.side_effect = stripe_lib.error.InvalidRequestError(
            "already succeeded", None
        )
        stripe.return_value.PaymentIntent.retrieve.return_value = MagicMock(status="succeeded")
        with pytest.raises(BillingError, match="just went through"):
            services.bill_to_account(lead, group, po_number="", user=None)

    assert not Lead.objects.get(pk=lead.pk).on_account
