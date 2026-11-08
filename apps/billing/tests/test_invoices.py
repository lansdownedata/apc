"""Invoice and InvoiceLine (APC-106): a trip is on at most one invoice, and a group that
has been invoiced cannot be removed."""

from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.billing import invoices, services
from apps.billing.factories import AccountGroupFactory, InvoiceFactory, InvoiceLineFactory
from apps.leads.factories import VehicleTypeFactory
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def test_a_trip_is_on_at_most_one_invoice() -> None:
    first = InvoiceLineFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLineFactory(reservation=first.reservation)


def test_group_with_invoices_cannot_be_deleted() -> None:
    group = AccountGroupFactory(is_default=False)
    AccountGroupFactory(account=group.account, is_default=True)
    InvoiceFactory(group=group)
    with pytest.raises(services.BillingError, match="invoices"):
        services.delete_group(group)
    with pytest.raises(ProtectedError):
        group.delete()


def test_invoice_label() -> None:
    draft = InvoiceFactory(number="")
    assert draft.label == f"Invoice (draft #{draft.pk})"
    assert InvoiceFactory(number="1042").label == "Invoice 1042"


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        ("due_on_receipt", date(2026, 1, 20)),
        ("net_15", date(2026, 2, 4)),
        ("net_30", date(2026, 2, 19)),
        ("net_45", date(2026, 3, 6)),
    ],
)
def test_due_date_per_term(terms: str, expected: date) -> None:
    assert invoices.due_date(date(2026, 1, 20), terms) == expected


def test_due_date_unknown_terms_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown payment terms 'net_60'"):
        invoices.due_date(date(2026, 1, 20), "net_60")


def _trip(stops: list[tuple[str, str]], **kwargs) -> Reservation:
    trip = ReservationFactory(stops=[], **kwargs)
    for i, (name, address) in enumerate(stops):
        trip.stops.create(sequence=i, name=name, address=address)
    return trip


def test_line_description_hourly_with_gratuity() -> None:
    trip = _trip(
        [("Union Station", "50 Massachusetts Ave NE"), ("The Wharf", "760 Maine Ave SW")],
        trip_type=Reservation.TripType.HOURLY,
        rate=230,
        hours=4,
        min_hours=0,
        gratuity_pct=20,
        vehicle=VehicleTypeFactory(name="Mini Coach"),
        pickup_date=date(2026, 10, 18),
    )
    assert invoices.line_description(trip) == (
        f"Trip #{trip.reference} · Hourly, 4 hrs · Sun Oct 18 · Mini Coach\n"
        "Union Station → The Wharf · Trip $920.00 · Gratuity $184.00 · Total $1,104.00"
    )


def test_line_description_overtime_discount_transfer_no_vehicle() -> None:
    trip = _trip(
        [("", "1600 Pennsylvania Ave NW, Washington, DC"), ("The Wharf", "")],
        trip_type=Reservation.TripType.TRANSFER,
        rate=200,
        hours=1,
        overtime_hours=1,
        discount_flat=50,
        vehicle=None,
        pickup_date=date(2026, 10, 18),
    )
    first, second = invoices.line_description(trip).split("\n")
    assert first == f"Trip #{trip.reference} · Transfer + 1 hr overtime · Sun Oct 18"
    assert second.startswith("1600 Pennsylvania Ave NW → The Wharf · Trip $")
    assert f"Trip ${invoices.trip_figure(trip):,.2f} (after $50.00 discount)" in second
    assert "Gratuity" not in second
    assert second.endswith(f"Total ${trip.line_total:,.2f}")


@pytest.mark.parametrize(("hours", "wording"), [(2, "+ 2 hrs overtime"), (1, "+ 1 hr overtime")])
def test_overtime_hours_wording(hours: int, wording: str) -> None:
    trip = _trip(
        [("A", ""), ("B", "")],
        trip_type=Reservation.TripType.HOURLY,
        rate=100,
        hours=4,
        min_hours=0,
        overtime_hours=Decimal(hours),
    )
    assert wording in invoices.line_description(trip).split("\n")[0]
