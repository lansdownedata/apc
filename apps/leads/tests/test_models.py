from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import HourlyReservationFactory, TransferReservationFactory

pytestmark = pytest.mark.django_db


def test_new_lead_defaults_to_new():
    assert LeadFactory().status == Lead.Status.NEW


def test_quote_no_derived_from_pk():
    lead = LeadFactory()
    assert lead.quote_no == f"Q-{1040 + lead.pk}"


def test_quote_total_sums_reservation_line_totals():
    lead = LeadFactory()
    TransferReservationFactory(lead=lead, base_rate=Decimal("900"))
    HourlyReservationFactory(
        lead=lead, hours=Decimal("6"), hourly_rate=Decimal("295"), min_hours=Decimal("4")
    )  # 6 * 295 = 1770
    assert lead.quote_total == Decimal("2670.00")
    assert lead.reservation_count == 2


def test_open_pipeline_value_excludes_booked_and_lost():
    quoted = LeadFactory(status=Lead.Status.QUOTED)
    TransferReservationFactory(lead=quoted, base_rate=Decimal("500"))
    booked = LeadFactory(status=Lead.Status.BOOKED)
    TransferReservationFactory(lead=booked, base_rate=Decimal("999"))
    assert Lead.objects.open_pipeline_value() == Decimal("500.00")


def test_str_includes_quote_no():
    lead = LeadFactory()
    assert lead.quote_no in str(lead)
