"""The spreadsheet export (`/account/trips/export.csv`)."""

import csv
import io
from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.customers import exports
from apps.customers.factories import CustomerAccountFactory
from apps.customers.selectors import TripFilter
from apps.leads.factories import LeadFactory
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


def test_an_unpriced_trip_has_blank_money(account, make_trip):
    make_trip(account, lead__status=Lead.Status.NEW, rate=0, pickup_date=_day(5))
    cells = _cells(account)
    assert cells["Total"] == "" and cells["Paid"] == ""


def test_paid_is_what_the_trip_allocated_and_the_card_is_named(account, make_trip):
    trip = _booked(
        account, make_trip, rate=Decimal("200.00"), hours=1, booked_total=Decimal("200.00")
    )
    plan = PaymentPlanFactory(
        lead=trip.lead,
        quote_total=trip.line_total,
        card_brand="visa",
        card_last4="4242",
    )
    ChargeFactory(
        plan=plan,
        kind=Charge.Kind.DEPOSIT,
        status=Charge.Status.SUCCEEDED,
        amount=Decimal("100.00"),
    )
    cells = _cells(account)
    assert cells["Paid"] == "100.00"
    assert cells["Payment method"] == "Visa ending 4242"


def test_an_order_billed_to_an_account_says_account(account, make_trip):
    _booked(account, make_trip, lead__billing_group=AccountGroupFactory())
    cells = _cells(account)
    assert cells["Payment method"] == "Account"


def test_export_respects_filter_and_is_not_paged(account, make_trip):
    for i in range(60):
        _booked(account, make_trip, days=10 + i)
    _booked(account, make_trip, days=-5, trip_status=S.DONE)
    rows = _rows(exports.trips_csv(account, f=TripFilter(tab="upcoming")))
    assert len(rows) == 61  # header + 60 upcoming, not the 50 a page shows
    past = _rows(exports.trips_csv(account, f=TripFilter(tab="past")))
    assert len(past) == 2


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

    monkeypatch.setattr(views, "MAX_EXPORT_IDS", 2)
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


# Session, account, last seen, the trips with their stops and passengers, the charges (7);
# then `trip_money.allocation` per order: two prepaid sums, frozen shares, the order's trips.
# Measured 27 for 60 trips on 5 orders: it grows with orders, never with trips.
BASE_QUERIES = 7
PER_ORDER = 4


def test_export_query_count_with_60_rows(
    account, client_as, make_trip, django_assert_max_num_queries
):
    leads = [LeadFactory(contact=account.contact, status=Lead.Status.BOOKED) for _ in range(5)]
    for lead in leads:
        PaymentPlanFactory(lead=lead, quote_total=Decimal("500.00"))
    for i in range(60):
        trip = _booked(account, make_trip, days=10 + i, lead=leads[i % 5])
        PassengerFactory(reservation=trip, is_lead=True)
    client = client_as(account)
    with django_assert_max_num_queries(BASE_QUERIES + PER_ORDER * 5):
        response = client.get(URL, {"tab": "all"})
    rows = _rows(response.content.decode("utf-8-sig"))[1:]
    assert sum(1 for row in rows if row[0]) == 60  # the leads' own undated trips are extra
