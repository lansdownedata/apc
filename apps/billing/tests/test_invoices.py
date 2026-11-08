"""Invoice and InvoiceLine (APC-106): a trip is on at most one invoice, and a group that
has been invoiced cannot be removed."""

import re
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import invoices, services
from apps.billing.factories import AccountGroupFactory, InvoiceFactory, InvoiceLineFactory
from apps.billing.models import Invoice
from apps.billing.tests._helpers import _processed_trip
from apps.contacts.factories import ContactFactory
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview

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


def test_line_description_single_stop_has_no_arrow() -> None:
    trip = _trip(
        [("Union Station", "50 Massachusetts Ave NE")],
        trip_type=Reservation.TripType.HOURLY,
        rate=100,
        hours=4,
        min_hours=0,
    )
    second = invoices.line_description(trip).split("\n")[1]
    assert "→" not in second
    assert second.startswith("Union Station · Trip $")


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


# --- selectors -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


@pytest.fixture
def accountant():
    return UserFactory(can_manage_payments=True)


def test_ready_requires_processed_on_account_uninvoiced(accountant) -> None:
    ready = _processed_trip(accountant=accountant)
    card = _processed_trip(accountant=accountant)
    Lead.objects.filter(pk=card.lead_id).update(billing_group=None)
    unprocessed = _processed_trip(accountant=accountant)
    TripReview.objects.filter(reservation=unprocessed).update(processed_at=None)
    invoiced = _processed_trip(accountant=accountant)
    InvoiceLineFactory(reservation=invoiced)
    found = set(invoices.ready_queryset(invoices.BillingFilters()).values_list("pk", flat=True))
    assert found == {ready.pk}


def test_lost_order_trips_are_not_ready_but_invoiced_rows_remain(accountant) -> None:
    trip = _processed_trip(accountant=accountant)
    group = trip.lead.billing_group
    line = InvoiceLineFactory(reservation=trip, invoice=InvoiceFactory(group=group))
    Lead.objects.filter(pk=trip.lead_id).update(status="lost")
    assert invoices.ready_count() == 0
    assert invoices.open_invoices(group)[0].lines.count() == 1
    assert line.invoice_id == invoices.open_invoices(group)[0].pk


def test_ready_accounts_groups_and_totals(accountant) -> None:
    contact = ContactFactory()
    zed = AccountGroupFactory(account__contact=contact, account__name="Zed", name="B group")
    ann = AccountGroupFactory(account__name="Ann", name="A group")
    zed2 = AccountGroupFactory(account=zed.account, name="A2 group", is_default=False)
    t1 = _processed_trip(zed, accountant=accountant)
    t2 = _processed_trip(zed2, accountant=accountant)
    t3 = _processed_trip(ann, accountant=accountant)
    rows = invoices.ready_accounts(invoices.BillingFilters())
    assert [r.account.name for r in rows] == ["Ann", "Zed"]
    assert rows[0].total == t3.line_total
    assert [g.group.name for g in rows[1].groups] == ["A2 group", "B group"]
    assert [g.total for g in rows[1].groups] == [t2.line_total, t1.line_total]
    assert rows[1].total == t1.line_total + t2.line_total
    assert all(hasattr(t, "route_short") for g in rows[1].groups for t in g.trips)


def test_date_range_and_search_filters(accountant) -> None:
    today = timezone.localdate()
    recent = _processed_trip(accountant=accountant, pickup_date=today - timedelta(days=1))
    old = _processed_trip(accountant=accountant, pickup_date=today - timedelta(days=40))
    ids = lambda f: {  # noqa: E731
        t.pk for r in invoices.ready_accounts(f) for g in r.groups for t in g.trips
    }
    assert ids(invoices.BillingFilters()) == {recent.pk, old.pk}
    assert ids(invoices.BillingFilters(date_from=today - timedelta(days=10))) == {recent.pk}
    assert ids(invoices.BillingFilters(date_to=today - timedelta(days=10))) == {old.pk}
    assert ids(invoices.BillingFilters(q=recent.reference)) == {recent.pk}
    name = old.lead.contact.name
    assert ids(invoices.BillingFilters(q=name[:4])) >= {old.pk}
    mine = invoices.BillingFilters.from_query({"q": " x ", "from": "2026-01-02", "to": "bad"})
    assert (mine.q, mine.date_from, mine.date_to) == ("x", date(2026, 1, 2), None)


def _build(accountant, trips: int, groups: int) -> None:
    made = [AccountGroupFactory() for _ in range(groups)]
    for i in range(trips):
        _processed_trip(made[i % groups], accountant=accountant)


def _count(filters) -> int:
    with CaptureQueriesContext(connection) as ctx:
        invoices.ready_accounts(filters)
    return len(ctx)


def test_ready_accounts_is_flat(accountant) -> None:
    _build(accountant, 1, 1)
    one = _count(invoices.BillingFilters())
    _build(accountant, 29, 6)
    assert _count(invoices.BillingFilters()) == one


# --- services: create and void ---------------------------------------------------------------


def _create(group, trips, **kw):
    kw.setdefault("invoice_date", date(2026, 3, 1))
    kw.setdefault("po_number", "")
    kw.setdefault("memo", "")
    kw.setdefault("user", None)
    return invoices.create_invoice(group, [t.pk for t in trips], **kw)


def test_create_freezes_lines_and_total(accountant) -> None:
    group = AccountGroupFactory(terms="net_30")
    first = _processed_trip(group, accountant=accountant)
    second = _processed_trip(group, accountant=accountant)
    Lead.objects.filter(pk=first.lead_id).update(po_number="PO-7")

    invoice = _create(group, [first, second], user=accountant)

    assert invoice.lines.count() == 2
    for trip in (first, second):
        assert invoice.lines.get(reservation=trip).amount == trip.line_total
    assert invoice.total == first.line_total + second.line_total
    assert invoice.due_date == date(2026, 3, 31)
    assert invoice.po_number == "PO-7"
    assert invoice.created_by == accountant
    assert invoice.status == invoice.Status.OPEN


def test_create_po_required_rule(accountant) -> None:
    group = AccountGroupFactory(po_required=True)
    trip = _processed_trip(group, accountant=accountant)
    with pytest.raises(invoices.InvoiceError, match="This group needs a PO number."):
        _create(group, [trip])
    assert not Invoice.objects.exists()
    assert _create(group, [trip], po_number="  PO-1 ").po_number == "PO-1"


def test_create_refuses_empty_selection() -> None:
    with pytest.raises(invoices.InvoiceError, match="Pick at least one trip."):
        invoices.create_invoice(
            AccountGroupFactory(),
            [],
            invoice_date=date(2026, 3, 1),
            po_number="",
            memo="",
            user=None,
        )


def test_create_refuses_trips_outside_the_group(accountant) -> None:
    group = AccountGroupFactory()
    mine = _processed_trip(group, accountant=accountant)
    other = _processed_trip(accountant=accountant)
    with pytest.raises(invoices.InvoiceError, match="processed, billed to this group"):
        _create(group, [mine, other])
    assert not Invoice.objects.exists()


def test_create_refuses_a_trip_already_invoiced(accountant) -> None:
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    other = _processed_trip(group, accountant=accountant)
    existing = InvoiceLineFactory(
        reservation=trip, invoice=InvoiceFactory(group=group, number="1042")
    )
    with pytest.raises(invoices.InvoiceError, match="Pick trips that are processed"):
        _create(group, [trip, other])
    assert Invoice.objects.count() == 1
    assert Invoice.objects.get() == existing.invoice


def test_create_names_the_invoice_when_the_insert_collides(accountant) -> None:
    """The lock normally catches an invoiced trip; if one slips through, the OneToOne does."""
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    InvoiceLineFactory(reservation=trip, invoice=InvoiceFactory(group=group, number="1042"))
    ready = Reservation.objects.filter(pk=trip.pk)
    with patch.object(invoices, "_lock_ready", return_value=list(ready)):
        with pytest.raises(
            invoices.InvoiceError, match=f"Trip #{trip.reference} is already on Invoice 1042"
        ):
            _create(group, [trip])
    assert Invoice.objects.count() == 1


def test_create_lock_names_only_the_reservation_row(accountant) -> None:
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    with CaptureQueriesContext(connection) as ctx:
        _create(group, [trip])
    locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
    assert locks
    assert all("FOR UPDATE OF" in sql for sql in locks)


def test_price_edit_after_invoice_does_not_move_the_line(accountant) -> None:
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = _create(group, [trip])
    before = invoice.total
    Reservation.objects.filter(pk=trip.pk).update(hours=5)
    invoice.refresh_from_db()
    assert invoice.lines.get().amount == before
    assert invoice.total == before


def test_void_frees_the_trips(accountant) -> None:
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = _create(group, [trip])
    assert not invoices.ready_queryset(invoices.BillingFilters()).filter(pk=trip.pk).exists()

    invoices.void_invoice(invoice, reason="Wrong PO", user=accountant)

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID
    assert invoice.lines.count() == 0
    assert invoice.void_reason == "Wrong PO"
    assert invoice.voided_by == accountant
    assert invoice.voided_at is not None
    assert invoices.ready_queryset(invoices.BillingFilters()).filter(pk=trip.pk).exists()
    with pytest.raises(invoices.InvoiceError, match="This invoice is already void."):
        invoices.void_invoice(invoice, reason="again", user=accountant)


def test_void_needs_reason() -> None:
    with pytest.raises(invoices.InvoiceError, match="Say why the invoice is voided."):
        invoices.void_invoice(InvoiceFactory(), reason="  ", user=None)


def test_void_refuses_an_invoice_in_quickbooks() -> None:
    invoice = InvoiceFactory(qbo_id="7")
    with pytest.raises(invoices.InvoiceError, match="Voiding it there is APC-107."):
        invoices.void_invoice(invoice, reason="oops", user=None)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN


def test_invoicing_posts_nothing(accountant) -> None:
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    before = ledger.order_balances(trip.lead)
    invoice = _create(group, [trip])
    assert ledger.order_balances(trip.lead) == before
    invoices.void_invoice(invoice, reason="redo", user=accountant)
    assert ledger.order_balances(trip.lead) == before


def test_unlock_refuses_an_invoiced_trip(accountant) -> None:
    from apps.reservations import processing

    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = _create(group, [trip])
    with pytest.raises(processing.ProcessingError, match=re.escape(f"On {invoice.label}. Void it")):
        processing.unlock(trip, user=accountant, reason="price check")
    assert TripReview.objects.get(reservation=trip).is_processed


def test_unlock_allowed_after_void(accountant) -> None:
    from apps.reservations import processing

    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = _create(group, [trip])
    invoices.void_invoice(invoice, reason="test", user=accountant)
    review = processing.unlock(trip, user=accountant, reason="price check")
    assert not review.is_processed


def test_add_charge_refuses_an_invoiced_trip(accountant) -> None:
    from apps.reservations import processing
    from apps.reservations.models import TripExtra

    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = _create(group, [trip])
    before = Reservation.objects.get(pk=trip.pk).line_total
    with pytest.raises(
        processing.ProcessingError,
        match=re.escape(f"On {invoice.label}. Void it, then add the charge."),
    ):
        processing.add_charge(
            trip, user=accountant, amount="50", label="Stain on the seat", collect="invoice"
        )
    assert not TripExtra.objects.filter(reservation=trip).exists()
    assert Reservation.objects.get(pk=trip.pk).line_total == before


def test_invoice_admin_keeps_the_frozen_fields_read_only(rf) -> None:
    from django.contrib.admin.sites import site

    model_admin = site._registry[Invoice]
    invoice = InvoiceFactory()
    request = rf.get("/")
    request.user = UserFactory(is_superuser=True, is_staff=True)
    readonly = set(model_admin.get_readonly_fields(request, invoice))
    assert {"status", "total", "group", "number"} <= readonly
    assert model_admin.list_select_related == ("group__account",)
