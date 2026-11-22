"""The spreadsheet export (`/account/trips/export.csv`)."""

import csv
import io
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.customers import exports, trip_list
from apps.customers.factories import CustomerAccountFactory
from apps.customers.trip_list import TripFilter
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import PassengerFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
URL = "/account/trips/export.csv"
HEADER = [
    "Date",
    "Time",
    "Time zone",
    "Confirmation",
    "Status",
    "Passenger",
    "Pickup",
    "Drop-off",
    "Vehicle",
    "Your reference",
    "Total",
    "Paid",
    "Payment method",
]


def _day(days: int):
    return timezone.localdate() + timedelta(days=days)


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _cells(account) -> dict[str, str]:
    """The first trip's row of the whole-account export, by column."""
    row = _rows(exports.trips_csv(account, f=TripFilter(tab="all")))[1]
    return dict(zip(HEADER, row, strict=True))


def _booked(account, make_trip, days: int = 10, **kw) -> Reservation:
    kw.setdefault("lead__status", Lead.Status.BOOKED)
    kw.setdefault("trip_status", S.UNASSIGNED)
    kw.setdefault("pickup_time", time(6, 15))
    kw.setdefault("pickup_date", _day(days))
    return make_trip(account, **kw)


def test_export_has_exact_header(account):
    assert _rows(exports.trips_csv(account))[0] == HEADER


def test_export_of_empty_filter_is_header_only(account, make_trip):
    _booked(account, make_trip)
    rows = _rows(exports.trips_csv(account, f=TripFilter(tab="all", q="nothing matches this")))
    assert rows == [HEADER]


def test_a_row_reads_as_a_spreadsheet_wants_it(account, make_trip):
    trip = _booked(
        account,
        make_trip,
        pickup_timezone="America/New_York",
        pickup_date=date(2027, 1, 15),
        stops=["2200 Clarendon Blvd, Arlington, VA 22201", "1 Main St, Reston, VA 20190"],
        la_confirmation="48213",
        customer_reference="Board offsite",
        rate=Decimal("200.00"),
        hours=1,
    )
    PassengerFactory(reservation=trip, name="Ada Lovelace", is_lead=True)
    cells = _cells(account)
    assert cells["Date"] == trip.pickup_date.isoformat()
    assert cells["Time"] == "6:15 AM"
    assert cells["Time zone"] == "EST"
    assert cells["Confirmation"] == "48213"
    assert cells["Passenger"] == "Ada Lovelace"
    assert cells["Pickup"] == "2200 Clarendon Blvd, Arlington, VA 22201"
    assert cells["Drop-off"] == "1 Main St, Reston, VA 20190"
    assert cells["Vehicle"] == trip.vehicle.name
    assert cells["Your reference"] == "Board offsite"
    assert cells["Total"] == f"{trip.line_total:.2f}"
    assert cells["Paid"] == "0.00"
    assert cells["Payment method"] == ""


def test_a_draft_rate_on_a_new_lead_has_blank_money_until_the_quote_is_sent(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, rate=185, pickup_date=_day(5))
    cells = _cells(account)
    assert cells["Total"] == "" and cells["Paid"] == ""

    trip.lead.status = Lead.Status.QUOTED
    trip.lead.save(update_fields=["status"])
    assert _cells(account)["Total"] == f"{trip.line_total:.2f}"


def test_an_unpriced_trip_has_blank_money(account, make_trip):
    make_trip(account, lead__status=Lead.Status.NEW, rate=0, pickup_date=_day(5))
    cells = _cells(account)
    assert cells["Total"] == "" and cells["Paid"] == ""


def test_paid_is_what_the_trip_allocated_and_the_card_is_named(account, make_trip):
    trip = _booked(
        account, make_trip, rate=Decimal("200.00"), hours=1, booked_total=Decimal("200.00")
    )
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=trip.line_total)
    _card_charge(plan, "visa", "4242")
    cells = _cells(account)
    assert cells["Paid"] == "100.00"
    assert cells["Payment method"] == "Visa ending 4242"


def _card_charge(plan, brand: str = "", last4: str = "", **kw) -> Charge:
    kw.setdefault("kind", Charge.Kind.DEPOSIT)
    kw.setdefault("status", Charge.Status.SUCCEEDED)
    kw.setdefault("amount", Decimal("100.00"))
    return ChargeFactory(plan=plan, card_brand=brand, card_last4=last4, **kw)


def test_the_card_on_file_is_not_named_when_nothing_was_paid(account, make_trip):
    """The order's card on file is the one the next charge will use, not one that paid."""
    trip = _booked(account, make_trip)
    plan = PaymentPlanFactory(lead=trip.lead, card_brand="visa", card_last4="4242")
    _card_charge(plan, "visa", "4242", status=Charge.Status.FAILED)
    assert _cells(account)["Payment method"] == ""


def test_a_payment_with_no_card_on_record_says_card(account, make_trip):
    trip = _booked(account, make_trip)
    plan = PaymentPlanFactory(lead=trip.lead, card_brand="visa", card_last4="4242")
    _card_charge(plan)
    assert _cells(account)["Payment method"] == "Card"


def test_an_order_paid_by_two_cards_names_neither(account, make_trip):
    trip = _booked(account, make_trip)
    plan = PaymentPlanFactory(lead=trip.lead)
    _card_charge(plan, "visa", "4242")
    _card_charge(plan, "amex", "0005", kind=Charge.Kind.BALANCE)
    assert _cells(account)["Payment method"] == "Card"


def test_a_refund_does_not_change_the_card_that_paid(account, make_trip):
    trip = _booked(account, make_trip)
    plan = PaymentPlanFactory(lead=trip.lead)
    _card_charge(plan, "visa", "4242")
    _card_charge(plan, kind=Charge.Kind.REFUND, amount=Decimal("10.00"))
    assert _cells(account)["Payment method"] == "Visa ending 4242"


def test_an_order_billed_to_an_account_says_account(account, make_trip):
    _booked(account, make_trip, lead__billing_group=AccountGroupFactory())
    cells = _cells(account)
    assert cells["Payment method"] == "Account"


def test_paid_on_account_is_what_was_charged_which_is_nothing(account, make_trip):
    """Invoiced to the account: no card money, so Paid is 0.00 beside the trip's price."""
    trip = _booked(
        account,
        make_trip,
        lead__billing_group=AccountGroupFactory(),
        rate=Decimal("200.00"),
        hours=1,
        booked_total=Decimal("200.00"),
    )
    cells = _cells(account)
    assert (cells["Total"], cells["Paid"]) == (f"{trip.line_total:.2f}", "0.00")
    assert cells["Payment method"] == "Account"


def test_paid_is_net_of_a_refund_on_the_trip(account, make_trip):
    trip = _booked(
        account, make_trip, rate=Decimal("200.00"), hours=1, booked_total=Decimal("200.00")
    )
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=trip.line_total)
    for kind, amount, on in (
        (Charge.Kind.DEPOSIT, "100.00", None),
        (Charge.Kind.BALANCE, "100.00", None),
        (Charge.Kind.REFUND, "50.00", trip),
    ):
        ChargeFactory(
            plan=plan,
            kind=kind,
            status=Charge.Status.SUCCEEDED,
            amount=Decimal(amount),
            reservation=on,
        )

    assert _cells(account)["Paid"] == "150.00"


def test_paid_is_net_of_a_refund_on_the_whole_order(account, make_trip):
    """An order-level refund comes off the prepaid pool before it is shared out."""
    trip = _booked(
        account, make_trip, rate=Decimal("200.00"), hours=1, booked_total=Decimal("200.00")
    )
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=trip.line_total)
    for kind, amount in ((Charge.Kind.DEPOSIT, "200.00"), (Charge.Kind.REFUND, "200.00")):
        ChargeFactory(plan=plan, kind=kind, status=Charge.Status.SUCCEEDED, amount=Decimal(amount))

    assert _cells(account)["Paid"] == "0.00"


def test_export_respects_filter_and_is_not_paged(account, make_trip):
    for i in range(60):
        _booked(account, make_trip, days=10 + i)
    _booked(account, make_trip, days=-5, trip_status=S.DONE)
    rows = _rows(exports.trips_csv(account, f=TripFilter(tab="upcoming")))
    assert len(rows) == 61  # header + 60 upcoming, not the 50 a page shows
    past = _rows(exports.trips_csv(account, f=TripFilter(tab="past")))
    assert len(past) == 2


@pytest.mark.parametrize("tab", ["upcoming", "past", "requests", "all"])
def test_export_is_the_set_the_page_counts_in_the_pages_order(account, make_trips, make_trip, tab):
    make_trips(account, 21)
    _booked(account, make_trip, days=-2)  # left open after its day: Past on the page
    f = TripFilter(tab=tab, start=_day(-40))

    page = trip_list.trips(account, f)
    exported = _rows(exports.trips_csv(account, f=f))[1:]

    assert len(exported) == page["count"] > 0
    shown = [row["confirmation"] for group in page["groups"] for row in group["rows"]]
    assert [row[HEADER.index("Confirmation")] for row in exported] == shown


def test_export_of_ticked_ids_drops_other_customers(account, make_trip):
    mine = _booked(account, make_trip, customer_reference="mine")
    other = CustomerAccountFactory()
    theirs = _booked(other, make_trip, customer_reference="theirs")
    _booked(account, make_trip, customer_reference="not ticked")
    rows = _rows(exports.trips_csv(account, trip_ids=[mine.pk, theirs.pk]))
    assert [r[HEADER.index("Your reference")] for r in rows[1:]] == ["mine"]


@pytest.mark.parametrize("text", ["=HYPERLINK(1)", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd"])
def test_formula_like_text_is_neutralised_in_every_text_cell(account, make_trip, text):
    trip = _booked(account, make_trip, customer_reference=text, stops=[text, text])
    PassengerFactory(reservation=trip, name=text, is_lead=True)
    cells = _cells(account)
    assert cells["Your reference"] == "'" + text
    if not text.isspace() and text == text.strip():  # names and addresses are trimmed first
        for column in ("Passenger", "Pickup", "Drop-off"):
            assert cells[column] == "'" + text


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_safe_quotes_every_formula_start(start):
    assert exports._safe(f"{start}SUM(A1)") == f"'{start}SUM(A1)"


@pytest.mark.parametrize("text", ["", "Board offsite", "A-1", "1+1=2", "a@b.co", " =1", "'=1"])
def test_safe_leaves_ordinary_text_alone(text):
    assert exports._safe(text) == text


def test_cells_staff_or_an_import_wrote_are_neutralised_too(account, make_trip):
    """Confirmation comes from an import and Vehicle from Settings: guarded like the rest."""
    _booked(
        account,
        make_trip,
        la_confirmation="=HYPERLINK(1)",
        vehicle=VehicleTypeFactory(name="+Executive SUV"),
    )

    cells = _cells(account)

    assert cells["Confirmation"] == "'=HYPERLINK(1)"
    assert cells["Vehicle"] == "'+Executive SUV"
    assert cells["Status"] == "Confirmed"
    assert cells["Time zone"] in ("EST", "EDT")


def test_every_text_column_goes_through_the_guard(account, make_trip, monkeypatch):
    trip = _booked(account, make_trip, la_confirmation="48213", customer_reference="Board")
    PassengerFactory(reservation=trip, name="Ada Lovelace", is_lead=True)
    _card_charge(PaymentPlanFactory(lead=trip.lead), "visa", "4242")
    monkeypatch.setattr(exports, "_safe", lambda text: f"<{text}>")

    cells = _cells(account)

    money = {"Total", "Paid"}
    assert {name for name, cell in cells.items() if not cell.startswith("<")} == money
    assert cells["Payment method"] == "<Visa ending 4242>"


def test_commas_quotes_and_newlines_survive(account, make_trip):
    _booked(account, make_trip, customer_reference='A, "B"\nC')
    cells = _cells(account)
    assert cells["Your reference"] == 'A, "B"\nC'


# --- the view ----------------------------------------------------------------------------


def test_get_downloads_the_filtered_set_as_utf8_csv(account, client_as, make_trip):
    _booked(account, make_trip, customer_reference="Zoë")
    response = client_as(account).get(URL, {"tab": "upcoming"})
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == (
        f'attachment; filename="all-pro-charter-trips-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    body = response.content
    assert body.startswith(b"\xef\xbb\xbf")
    rows = _rows(body.decode("utf-8-sig"))
    assert rows[0] == HEADER and rows[1][HEADER.index("Your reference")] == "Zoë"


def test_post_needs_csrf(account, client_as, make_trip):
    trip = _booked(account, make_trip)
    client = Client(enforce_csrf_checks=True)
    client.cookies = client_as(account).cookies
    assert client.post(URL, {"ids": str(trip.pk)}).status_code == 403


def test_post_with_ids_drops_what_is_not_theirs(account, client_as, make_trip):
    mine = _booked(account, make_trip, customer_reference="mine")
    theirs = _booked(CustomerAccountFactory(), make_trip, customer_reference="theirs")
    response = client_as(account).post(URL, {"ids": [str(mine.pk), str(theirs.pk), "junk"]})
    rows = _rows(response.content.decode("utf-8-sig"))
    assert [r[HEADER.index("Your reference")] for r in rows[1:]] == ["mine"]


def test_ids_accepted_are_capped(account, client_as, make_trip, monkeypatch):
    from apps.customers import views

    monkeypatch.setattr(views, "MAX_TICKED_IDS", 2)
    trips = [_booked(account, make_trip, days=10 + i) for i in range(4)]
    response = client_as(account).post(URL, {"ids": ",".join(str(t.pk) for t in trips)})
    assert len(_rows(response.content.decode("utf-8-sig"))) == 3


def test_signed_out_is_sent_to_sign_in(client):
    response = client.get(URL)
    assert response.status_code == 302
    assert response["Location"].startswith("/account/login/")


def test_trips_page_offers_both_export_buttons(account, client_as, make_trip):
    _booked(account, make_trip)
    html = client_as(account).get("/account/trips/", {"tab": "all", "start": "2020-01-01"})
    html = html.content.decode()
    assert html.count("Export spreadsheet") == 2
    assert 'href="/account/trips/export.csv?tab=all&amp;start=2020-01-01"' in html


# Session, account, last seen, the trips with their stops and passengers, the trips' own
# charges (7); then `trip_money.allocations` for every order at once: prepaid sums, frozen
# shares, the orders' trips (3); and the cards that paid, for every order at once (1).
# Fixed: it grows with neither trips nor orders.
EXPORT_QUERIES = 11


def test_export_query_count_with_60_rows(
    account, client_as, make_trip, django_assert_max_num_queries
):
    leads = [LeadFactory(contact=account.contact, status=Lead.Status.BOOKED) for _ in range(20)]
    for lead in leads:
        PaymentPlanFactory(lead=lead, quote_total=Decimal("500.00"))
    for i in range(60):
        trip = _booked(account, make_trip, days=10 + i, lead=leads[i % 20])
        PassengerFactory(reservation=trip, is_lead=True)
    client = client_as(account)
    with django_assert_max_num_queries(EXPORT_QUERIES):
        response = client.get(URL, {"tab": "all"})
    rows = _rows(response.content.decode("utf-8-sig"))[1:]
    assert sum(1 for row in rows if row[0]) == 60  # the leads' own undated trips are extra
