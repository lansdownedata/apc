from decimal import Decimal

import pytest

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Company
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import HourlyReservationFactory, TransferReservationFactory

pytestmark = pytest.mark.django_db


def test_new_lead_defaults_to_new():
    assert LeadFactory().status == Lead.Status.NEW


def test_quote_no_is_the_apc_series():
    """Six digits from a 100000 base — reads as a real reference number and carries
    into LimoAnywhere / GNet better than the old four-digit "Q-" form."""
    lead = LeadFactory()
    assert lead.quote_no == f"APC-{100000 + lead.pk}"
    assert len(lead.quote_no.split("-")[1]) == 6


def test_an_unsaved_lead_has_no_quote_number():
    assert Lead().quote_no == "APC-—"


def test_quote_total_sums_reservation_line_totals():
    lead = LeadFactory()
    TransferReservationFactory(lead=lead, rate=Decimal("900"))
    HourlyReservationFactory(
        lead=lead, hours=Decimal("6"), rate=Decimal("295"), min_hours=Decimal("4")
    )  # 6 * 295 = 1770
    assert lead.quote_total == Decimal("2670.00")
    assert lead.reservation_count == 2


def test_open_pipeline_value_excludes_booked_and_lost():
    quoted = LeadFactory(status=Lead.Status.QUOTED)
    TransferReservationFactory(lead=quoted, rate=Decimal("500"))
    booked = LeadFactory(status=Lead.Status.BOOKED)
    TransferReservationFactory(lead=booked, rate=Decimal("999"))
    assert Lead.objects.open_pipeline_value() == Decimal("500.00")


def test_str_includes_quote_no():
    lead = LeadFactory()
    assert lead.quote_no in str(lead)


def test_effective_billing_contact_resolution_order(db):
    booker = ContactFactory(name="Assistant")
    ap = ContactFactory(name="AP Dept")
    company = Company.objects.create(name="BigCo", billing_contact=ap)
    booker.company = company
    booker.save()
    lead = LeadFactory(contact=booker)

    # no per-lead override → company's billing contact
    assert lead.effective_billing_contact == ap

    # per-lead override wins
    override = ContactFactory(name="Override")
    lead.billing_contact = override
    assert lead.effective_billing_contact == override

    # no company billing contact and no override → the booking contact
    company.billing_contact = None
    company.save()
    lead.billing_contact = None
    assert lead.effective_billing_contact == booker
