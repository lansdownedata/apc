from decimal import Decimal

import pytest

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def test_defaults_to_an_offered_manual_assignment():
    a = AssignmentFactory()
    assert a.status == Assignment.Status.OFFERED
    assert a.channel == Assignment.Channel.MANUAL
    assert a.resolved_at is None
    assert a.offered_at is not None


def test_offered_and_confirmed_are_active_others_are_not():
    active = {Assignment.Status.OFFERED, Assignment.Status.CONFIRMED}
    for status in Assignment.Status:
        a = AssignmentFactory(status=status)
        assert a.is_active is (status in active), status


def test_margin_is_customer_total_minus_payout():
    # rate 185 x 1 hour, no gratuity/discount -> line_total 185.00
    res = ReservationFactory(rate=185, hours=1)
    a = AssignmentFactory(reservation=res, payout=Decimal("140.00"))
    assert a.margin == Decimal("45.00")


def test_str_names_the_vendor_and_status():
    a = AssignmentFactory(vendor=VendorFactory(name="Capital Chauffeurs"))
    assert "Capital Chauffeurs" in str(a)
    assert "Offered" in str(a)
