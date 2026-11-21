"""The trip receipt PDF, `/account/trips/<pk>/receipt.pdf` (spec §7, mockup 4): when a trip
has one, what it says, that its money is the trip's own, and that the PDF is one page."""

import csv
import io
import re
import zlib
from datetime import UTC, date, datetime, time
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirportFactory
from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.customers import exports, receipts, selectors
from apps.customers.factories import CustomerAccountFactory
from apps.customers.selectors import TripFilter
from apps.leads import services as lead_services
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import trip_money
from apps.payments.factories import ChargeFactory
from apps.payments.models import Charge, PaymentPlan
from apps.reservations import processing
from apps.reservations.factories import (
    PassengerFactory,
    ReservationFactory,
    StopFactory,
    TripExtraFactory,
)
from apps.reservations.models import Reservation, TripReview

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
NY = "America/New_York"
PROCESSED = datetime(2026, 11, 19, 3, 30, tzinfo=UTC)  # Nov 18 in New York and Los Angeles


def _url(trip: Reservation) -> str:
    return reverse("customers:receipt", args=[trip.pk])


def _booked(account, make_trip, **kw) -> Reservation:
    """A trip on an order booked through `book_lead` (which freezes `booked_total`)."""
    kw.setdefault("pickup_date", date(2026, 11, 18))
    kw.setdefault("pickup_time", time(6, 15))
    kw.setdefault("pickup_timezone", NY)
    trip = make_trip(account, lead__status=Lead.Status.QUOTED, **kw)
    lead_services.book_lead(trip.lead)
    trip.refresh_from_db()
    return trip


def _pay(trip: Reservation, amount, *, at: datetime, **kw) -> Charge:
    """A succeeded charge on the trip's order, by Visa 4242 unless told otherwise."""
    kw.setdefault("kind", Charge.Kind.DEPOSIT)
    kw.setdefault("card_brand", "visa")
    kw.setdefault("card_last4", "4242")
    plan = PaymentPlan.objects.get(lead_id=trip.lead_id)
    return ChargeFactory(
        plan=plan,
        amount=Decimal(amount),
        status=Charge.Status.SUCCEEDED,
        captured_at=at,
        **kw,
    )


def _finish(trip: Reservation, *, processed: bool = True) -> Reservation:
    """Completed, and processed (final price locked) unless told otherwise."""
    trip.trip_status = S.DONE
    trip.save(update_fields=["trip_status"])
    TripReview.objects.create(reservation=trip, processed_at=PROCESSED if processed else None)
    return trip


def _cancel(trip: Reservation, status: str = S.CANCELLED) -> Reservation:
    trip.trip_status = status
    trip.cancelled_at = PROCESSED
    trip.save(update_fields=["trip_status", "cancelled_at"])
    return trip


def _scoped(account, trip: Reservation) -> Reservation:
    """The trip as every customer view gets it."""
    return selectors.trip_or_404(account, trip.pk)


def _paid_trip(account, make_trip, **kw) -> Reservation:
    """A completed, processed trip paid in full by a deposit and a balance."""
    trip = _booked(account, make_trip, **kw)
    half = (trip.line_total / 2).quantize(Decimal("0.01"))
    _pay(trip, half, at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _pay(
        trip,
        trip.line_total - half,
        kind=Charge.Kind.BALANCE,
        at=datetime(2026, 10, 29, 15, 0, tzinfo=UTC),
    )
    return _finish(trip)


def _pages(data: bytes) -> int:
    """Page objects in the PDF (`/Type /Page`, never the `/Pages` tree)."""
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))


def _text(data: bytes) -> str:
    """Every content stream of the PDF, inflated and read as the core fonts' encoding."""
    out = []
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out.append(zlib.decompress(raw).decode("cp1252", "replace"))
        except zlib.error:
            out.append(raw.decode("cp1252", "replace"))
    text = "\n".join(out)
    return text.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


_SHOWN = re.compile(r"BT ([-\d.]+) ([-\d.]+) Td(?: [-\d. ]+ rg)? \(((?:\\.|[^\\)])*)\) Tj ET")
PAGE_HEIGHT = 792  # Letter, in points
MARGIN = 54


def _placed(data: bytes) -> list[tuple[float, str]]:
    """(baseline y, text) of every piece of text drawn, page after page (y from the
    bottom of its page, as PDF counts it)."""
    out = []
    for match in _SHOWN.finditer(_text(data).replace("\\(", "(").replace("\\)", ")")):
        out.append((float(match.group(2)), match.group(3)))
    return out


# --- when a trip has a receipt -----------------------------------------------------------


def test_no_receipt_until_trip_is_processed(account, make_trip):
    trip = _booked(account, make_trip)
    assert not receipts.has_receipt(_scoped(account, trip))

    _finish(trip, processed=False)
    assert not receipts.has_receipt(_scoped(account, trip))

    TripReview.objects.filter(reservation=trip).update(processed_at=PROCESSED)
    assert receipts.has_receipt(_scoped(account, trip))


def test_fully_refunded_cancellation_has_no_receipt(account, make_trip):
    trip = _booked(account, make_trip)
    _pay(trip, trip.line_total, kind=Charge.Kind.TRIP, reservation=trip, at=PROCESSED)
    _pay(trip, trip.line_total, kind=Charge.Kind.REFUND, reservation=trip, at=PROCESSED)
    _cancel(trip)

    assert not receipts.has_receipt(_scoped(account, trip))


def test_cancelled_with_money_kept_has_one_cancellation_line(account, make_trip):
    """A plain cancellation is never processed: money kept on it (its own charge) is its
    receipt as soon as it is cancelled."""
    trip = _booked(account, make_trip)
    _pay(
        trip,
        "92.50",
        kind=Charge.Kind.TRIP,
        reservation=trip,
        at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
    )
    _cancel(trip)
    trip = _scoped(account, trip)

    assert receipts.has_receipt(trip)
    ctx = receipts.receipt_context(trip)
    assert ctx["lines"] == [("Cancellation charge", Decimal("92.50"))]
    assert ctx["total"] == Decimal("92.50")
    assert ctx["payments"] == [("Oct 28, 2026", "Visa ending 4242", Decimal("92.50"))]
    assert ctx["balance_due"] == Decimal("0.00")
    assert ctx["issued"] == "Nov 18, 2026"


def _kept_cancel(account, make_trip) -> Reservation:
    """A plain cancellation that kept its own $92.50 trip charge."""
    trip = _booked(account, make_trip)
    _pay(trip, "92.50", kind=Charge.Kind.TRIP, reservation=trip, at=PROCESSED)
    return _cancel(trip)


def _late_cancel(
    account, make_trip, *, processed: bool, settled: Decimal = Decimal("92.50")
) -> Reservation:
    """A late cancel that kept its $92.50 deposit share. Processing settles it to the
    trip's price, here cut to `settled` by a credit."""
    trip = _booked(account, make_trip)
    _pay(trip, "92.50", at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _cancel(trip, S.LATE_CANCEL)
    if processed:
        TripExtraFactory(
            reservation=trip,
            label="Late cancellation",
            amount=trip.line_total - settled,
            kind="credit",
        )
        trip.refresh_extras_total()
        TripReview.objects.create(reservation=trip, processed_at=PROCESSED)
    return trip


def test_unprocessed_late_cancel_has_no_receipt_yet(account, client_as, make_trip):
    trip = _late_cancel(account, make_trip, processed=False)
    client = client_as(account)

    assert not receipts.has_receipt(_scoped(account, trip))
    assert client.get(_url(trip)).status_code == 404
    html = client.get(reverse("customers:trip", args=[trip.pk])).content.decode()
    assert "Receipt available once your trip is finalised." in html
    assert "Download receipt" not in html


def test_processed_late_cancel_prints_the_settled_amount(account, make_trip):
    """Processing settled the late cancel at $120 (its price after the agent's credit) with
    a pay link still open for the rest: the receipt shows the settled amount, not the $92.50
    kept before processing, and what is still owed."""
    trip = _late_cancel(account, make_trip, processed=True, settled=Decimal("120.00"))
    trip = _scoped(account, trip)

    assert receipts.has_receipt(trip)
    ctx = receipts.receipt_context(trip)
    assert ctx["lines"] == [("Cancellation charge", Decimal("120.00"))]
    assert ctx["total"] == Decimal("120.00")
    assert ctx["payments"] == [("Oct 28, 2026", "Visa ending 4242", Decimal("92.50"))]
    assert ctx["balance_due"] == Decimal("27.50")
    assert ctx["issued"] == "Nov 18, 2026"


def _no_show(account, make_trip, *, processed: bool) -> Reservation:
    trip = _booked(account, make_trip, gratuity_pct=20)
    _pay(trip, trip.line_total, at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    trip.trip_status = S.NO_SHOW
    trip.save(update_fields=["trip_status"])
    TripReview.objects.create(reservation=trip, processed_at=PROCESSED if processed else None)
    return trip


def test_processed_no_show_prints_its_price_lines(account, client_as, make_trip):
    """Processing settles a no-show to the trip's price, so its receipt itemises it."""
    trip = _scoped(account, _no_show(account, make_trip, processed=True))

    assert receipts.has_receipt(trip)
    ctx = receipts.receipt_context(trip)
    assert ctx["lines"] == [(line.label, line.amount) for line in trip.price_lines()]
    assert [label for label, _ in ctx["lines"]] == ["Base fare", "Gratuity (20%)"]
    assert ctx["total"] == trip.line_total
    assert ctx["balance_due"] == Decimal("0.00")


def test_unprocessed_no_show_waits_for_processing(account, client_as, make_trip):
    trip = _no_show(account, make_trip, processed=False)
    client = client_as(account)

    assert not receipts.has_receipt(_scoped(account, trip))
    assert client.get(_url(trip)).status_code == 404
    html = client.get(reverse("customers:trip", args=[trip.pk])).content.decode()
    assert "Receipt available once your trip is finalised." in html


def test_no_receipt_for_nothing(account, make_trip):
    """No $0.00 receipts: a processed trip priced at nothing has none."""
    free = _finish(_booked(account, make_trip, rate=0))

    assert not receipts.has_receipt(_scoped(account, free))


@pytest.mark.parametrize(
    "build",
    [
        lambda a, m: _late_cancel(a, m, processed=False),
        lambda a, m: _late_cancel(a, m, processed=True),
        lambda a, m: _no_show(a, m, processed=False),
        lambda a, m: _no_show(a, m, processed=True),
        lambda a, m: _cancel(_booked(a, m)),
        lambda a, m: _kept_cancel(a, m),
        lambda a, m: _paid_trip(a, m),
        lambda a, m: _finish(_booked(a, m), processed=False),
    ],
    ids=[
        "late-cancel",
        "late-cancel-processed",
        "no-show",
        "no-show-processed",
        "cancelled-nothing-kept",
        "cancelled-money-kept",
        "completed-processed",
        "completed",
    ],
)
def test_list_and_download_agree(account, client_as, make_trip, build):
    trip = build(account, make_trip)
    client = client_as(account)

    page = client.get("/account/trips/", {"tab": "all"})
    row = next(r for g in page.context["groups"] for r in g["rows"] if r["pk"] == trip.pk)
    status = client.get(_url(trip)).status_code

    assert status == (200 if row["has_receipt"] else 404)
    assert row["has_receipt"] is receipts.has_receipt(_scoped(account, trip))


def test_multi_trip_order_shows_this_trips_share_only(account, make_trip):
    first_trip = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        pickup_date=date(2026, 11, 18),
        pickup_time=time(6, 15),
        pickup_timezone=NY,
    )
    second = ReservationFactory(
        lead=first_trip.lead, rate=300, pickup_date=date(2026, 11, 20), pickup_timezone=NY
    )
    lead_services.book_lead(first_trip.lead)
    first_trip.refresh_from_db()
    order_total = first_trip.line_total + second.line_total
    half = (order_total / 2).quantize(Decimal("0.01"))
    _pay(first_trip, half, at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _pay(first_trip, order_total - half, kind=Charge.Kind.BALANCE, at=PROCESSED)
    # Tolls added after the trip, charged to the trip itself; the other trip's own charge
    # and another customer's money must stay off this receipt.
    TripExtraFactory(reservation=first_trip, label="Tolls", amount=12)
    first_trip.refresh_extras_total()
    _pay(
        first_trip,
        "12.00",
        kind=Charge.Kind.TRIP,
        reservation=first_trip,
        at=datetime(2026, 11, 19, 16, 0, tzinfo=UTC),
        card_brand="mastercard",
        card_last4="5555",
    )
    _pay(second, "40.00", kind=Charge.Kind.TRIP, reservation=second, at=PROCESSED)
    other = _paid_trip(CustomerAccountFactory(), make_trip, rate=411)
    _finish(first_trip)
    _finish(second)

    ctx = receipts.receipt_context(_scoped(account, first_trip))

    assert sum(a for _, _, a in ctx["payments"]) == first_trip.line_total
    assert ctx["payments"] == [
        ("Oct 28, 2026", "Visa ending 4242", Decimal("185.00")),
        ("Nov 19, 2026", "Mastercard ending 5555", Decimal("12.00")),
    ]
    assert ctx["total"] == first_trip.line_total
    assert ctx["balance_due"] == Decimal("0.00")
    assert Decimal("40.00") not in [a for _, _, a in ctx["payments"]]
    assert not {other.line_total, other.line_total / 2} & {a for _, _, a in ctx["payments"]}


def test_a_refund_on_the_trip_is_a_negative_payment(account, make_trip):
    trip = _booked(account, make_trip)
    _pay(trip, trip.line_total, at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    TripExtraFactory(reservation=trip, label="Refund: late car", amount=20, kind="credit")
    trip.refresh_extras_total()
    _pay(
        trip,
        "20.00",
        kind=Charge.Kind.REFUND,
        reservation=trip,
        at=datetime(2026, 11, 19, 16, 0, tzinfo=UTC),
        refund_method=Charge.RefundMethod.CHECK,
        card_brand="",
        card_last4="",
    )
    _finish(trip)

    ctx = receipts.receipt_context(_scoped(account, trip))

    assert ctx["payments"][-1] == ("Nov 19, 2026", "Refund by check", Decimal("-20.00"))
    assert sum(a for _, _, a in ctx["payments"]) == trip.line_total
    assert ctx["balance_due"] == Decimal("0.00")


def test_imported_trip_without_payments_omits_payments(account, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        la_confirmation="48213",
        pickup_date=date(2026, 10, 21),
        pickup_time=time(15, 5),
        pickup_timezone=NY,
    )
    _finish(trip)
    trip = _scoped(account, trip)

    assert receipts.has_receipt(trip)
    ctx = receipts.receipt_context(trip)
    assert ctx["payments"] == []
    assert ctx["balance_due"] is None
    assert ctx["total"] == trip.line_total
    text = _text(receipts.render_pdf(ctx))
    assert "Payments" not in text
    assert "Balance due" not in text
    assert "Total" in text


def test_on_account_order_says_billed_to_account(account, make_trip):
    trip = make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        lead__billing_group=AccountGroupFactory(),
        pickup_date=date(2026, 11, 18),
        pickup_timezone=NY,
    )
    _finish(trip)

    ctx = receipts.receipt_context(_scoped(account, trip))

    assert ctx["payments"] == [("", "Billed to your account", trip.line_total)]
    assert "Billed to your account" in _text(receipts.render_pdf(ctx))


# --- what it says ------------------------------------------------------------------------


def test_receipt_context_carries_the_trip(account, make_trip, settings):
    settings.COMPANY_NAME = "All Pro Charter"
    settings.COMPANY_PHONE = "(703) 555-0142"
    settings.COMPANY_EMAIL = "billing@allprocharter.com"
    company = CompanyFactory(name="Meridian Advisory Group")
    billing = ContactFactory(name="Dana Whitfield", company=company, email="dana@example.com")
    trip = make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        lead__billing_contact=billing,
        vehicle=VehicleTypeFactory(name="Executive Sedan"),
        passengers=1,
        pickup_date=date(2026, 11, 18),
        pickup_time=time(6, 15),
        pickup_timezone=NY,
        customer_reference="Halvorsen engagement",
        gratuity_pct=20,
        rate=150,
        stops=[],
    )
    StopFactory(reservation=trip, sequence=0, address="2200 Clarendon Blvd, Arlington, VA")
    StopFactory(
        reservation=trip,
        sequence=1,
        name="",
        address="1 Saarinen Cir, Dulles, VA 20166",
        airport=AirportFactory(name="Washington Dulles International Airport", iata="IAD"),
    )
    PassengerFactory(reservation=trip, name="Dana Whitfield", is_lead=True)
    lead_services.book_lead(trip.lead)
    trip.refresh_from_db()
    _pay(trip, trip.line_total, at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _finish(trip)

    ctx = receipts.receipt_context(_scoped(account, trip))

    assert ctx["number"] == trip.reference
    assert ctx["issued"] == "Nov 18, 2026"
    assert ctx["billed_to"] == {
        "name": "Dana Whitfield",
        "company": "Meridian Advisory Group",
        "email": "dana@example.com",
    }
    assert ctx["passenger"] == "Dana Whitfield"
    assert ctx["reference"] == "Halvorsen engagement"
    assert ctx["when"] == "Wednesday, November 18, 2026 at 6:15 AM EST"
    assert ctx["route_full"] == (
        "2200 Clarendon Blvd, Arlington, VA to Washington Dulles International Airport (IAD)"
    )
    assert ctx["vehicle_line"] == "Executive Sedan, 1 passenger, airport transfer"
    assert ctx["lines"] == [
        ("Base fare", Decimal("150.00")),
        ("Gratuity (20%)", Decimal("30.00")),
    ]
    assert ctx["total"] == Decimal("180.00")
    assert ctx["company"] == {
        "name": "All Pro Charter",
        "address": "",
        "phone": "(703) 555-0142",
        "email": "billing@allprocharter.com",
    }
    text = _text(receipts.render_pdf(ctx))
    for words in ("Trip receipt", trip.reference, "Halvorsen engagement", "Gratuity (20%)"):
        assert words in text


def test_pdf_is_a_pdf_and_one_page(account, make_trip):
    trip = _scoped(account, _paid_trip(account, make_trip))

    data = receipts.render_pdf(receipts.receipt_context(trip))

    assert data[:5] == b"%PDF-"
    assert _pages(data) == 1


def test_long_text_wraps_and_stays_one_page(account, make_trip):
    trip = _paid_trip(
        account,
        make_trip,
        customer_reference="R" * 60 + " cost centre 4471 " + "x" * 42,
        stops=[
            "1600 Pennsylvania Avenue NW, The White House Visitor Center, Washington, DC 20500",
            "Ronald Reagan Washington National Airport, Terminal 2, Arlington, VA 22202",
            "The Ritz-Carlton Tysons Corner, 1700 Tysons Boulevard, McLean, VA 22102",
        ],
    )
    for i in range(6):
        TripExtraFactory(reservation=trip, label=f"Parking and tolls, part {i}", amount=5)
    trip.refresh_extras_total()

    ctx = receipts.receipt_context(_scoped(account, trip))
    data = receipts.render_pdf(ctx)

    assert _pages(data) == 1
    text = _text(data)
    assert "Parking and tolls, part 5" in text
    assert "McLean" in text


def test_nothing_is_drawn_off_the_page(account, make_trip):
    """A coach full of named passengers, a long multi-stop route and a very long billing
    email: every line lands inside the page margins, on as many pages as it takes."""
    billing = ContactFactory(name="Accounts Payable", email="a" * 60 + "@" + "b" * 180 + ".com")
    trip = _paid_trip(
        account,
        make_trip,
        lead__billing_contact=billing,
        stops=[
            f"{100 + i} Very Long Avenue Northwest, Suite {i}, Building {i}, Washington, DC 20001"
            for i in range(40)
        ],
    )
    for i in range(56):
        PassengerFactory(reservation=trip, name=f"Passenger Number {i + 1}", is_lead=i == 0)

    data = receipts.render_pdf(receipts.receipt_context(_scoped(account, trip)))

    placed = _placed(data)
    assert placed
    for y, text in placed:
        assert MARGIN - 1 <= y <= PAGE_HEIGHT - MARGIN, (y, text)
    shown = " ".join(text for _, text in placed)
    assert "Passenger Number 1, Passenger Number 2" in shown
    assert "and 52 more" in shown
    assert "Washington, DC 20001" in shown
    assert "Balance due" in shown
    assert "Thank you for riding" in shown


def test_a_few_passengers_are_all_named(account, make_trip):
    trip = _paid_trip(account, make_trip)
    for i in range(5):
        PassengerFactory(reservation=trip, name=f"Rider {i + 1}", is_lead=i == 2)

    ctx = receipts.receipt_context(_scoped(account, trip))

    assert ctx["passenger"] == "Rider 3, Rider 1, Rider 2, Rider 4, Rider 5"


def test_reference_with_non_latin_characters_does_not_crash(account, make_trip):
    trip = _paid_trip(account, make_trip, customer_reference="Café – Zoë 東京")
    PassengerFactory(reservation=trip, name="Zoë 李", is_lead=True)

    data = receipts.render_pdf(receipts.receipt_context(_scoped(account, trip)))

    assert data[:5] == b"%PDF-"
    text = _text(data)
    assert "Café – Zoë ??" in text
    assert "Zoë ?" in text


# --- the download and where it is offered ------------------------------------------------


def test_download_is_a_pdf_attachment_named_by_the_reference(account, client_as, make_trip):
    trip = _paid_trip(account, make_trip)

    response = client_as(account).get(_url(trip))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == (
        f'attachment; filename="All-Pro-Charter-{trip.reference}.pdf"'
    )
    assert "no-store" in response["Cache-Control"]
    assert response.content[:5] == b"%PDF-"


def test_no_receipt_yet_is_404(account, client_as, make_trip):
    trip = _finish(_booked(account, make_trip), processed=False)

    assert client_as(account).get(_url(trip)).status_code == 404


def test_other_customers_receipt_is_404(account, client_as, make_trip):
    theirs = _paid_trip(CustomerAccountFactory(), make_trip)

    response = client_as(account).get(_url(theirs))

    assert response.status_code == 404
    assert client_as(account).get(reverse("customers:receipt", args=[10**9])).status_code == 404


def test_signed_out_download_goes_to_sign_in(client, account, make_trip):
    trip = _paid_trip(account, make_trip)

    response = client.get(_url(trip))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("customers:login"))


def test_trip_page_offers_the_receipt_or_says_when(account, client_as, make_trip):
    client = client_as(account)
    ready = _paid_trip(account, make_trip)
    waiting = _finish(_booked(account, make_trip), processed=False)
    upcoming = _booked(account, make_trip)

    html = client.get(reverse("customers:trip", args=[ready.pk])).content.decode()
    assert f'href="{_url(ready)}"' in html
    assert "Download receipt" in html
    assert "Receipt available once your trip is finalised." not in html

    html = client.get(reverse("customers:trip", args=[waiting.pk])).content.decode()
    assert "Download receipt" not in html
    assert "Receipt available once your trip is finalised." in html

    html = client.get(reverse("customers:trip", args=[upcoming.pk])).content.decode()
    assert "Download receipt" not in html
    assert "Receipt available once" not in html


def test_trips_list_links_the_pdf_or_shows_a_dash(account, client_as, make_trip):
    ready = _paid_trip(account, make_trip)
    waiting = _finish(_booked(account, make_trip), processed=False)

    result = selectors.trips(account, TripFilter(tab="past"))
    rows = {row["pk"]: row for group in result["groups"] for row in group["rows"]}
    assert rows[ready.pk]["has_receipt"] is True
    assert rows[waiting.pk]["has_receipt"] is False

    html = client_as(account).get("/account/trips/", {"tab": "past"}).content.decode()
    assert html.count(f'href="{_url(ready)}"') == 2  # the desktop table and the phone row
    assert f'href="{_url(waiting)}"' not in html
    assert ">PDF</a>" in html


def test_has_receipt_rows_cost_no_query_per_row(
    account, client_as, make_trip, django_assert_max_num_queries
):
    for _ in range(5):
        _paid_trip(account, make_trip)
    for _ in range(3):
        trip = _booked(account, make_trip)
        _pay(trip, "50.00", kind=Charge.Kind.TRIP, reservation=trip, at=PROCESSED)
        _cancel(trip)
    client = client_as(account)

    # As the Trips page's own budget: 8, plus 4 per order holding a cancelled trip.
    with django_assert_max_num_queries(8 + 4 * 3):
        response = client.get("/account/trips/", {"tab": "past"})
    rows = [row for group in response.context["groups"] for row in group["rows"]]
    assert len(rows) == 8
    assert all(row["has_receipt"] for row in rows)


def test_trip_row_has_receipt_matches_has_receipt(account, make_trip):
    done = _paid_trip(account, make_trip)
    kept = _booked(account, make_trip)
    _pay(kept, "50.00", kind=Charge.Kind.TRIP, reservation=kept, at=PROCESSED)
    _cancel(kept)
    refunded = _cancel(_booked(account, make_trip))

    for trip in (done, kept, refunded):
        scoped = _scoped(account, trip)
        assert selectors.trip_row(scoped)["has_receipt"] is receipts.has_receipt(scoped)


# --- the customer's figure for a cancelled trip --------------------------------------------


def _figures(account, client_as, trip: Reservation) -> dict[str, Decimal | None]:
    """The trip's total as the trip page, the Trips row, the spreadsheet and the receipt
    each show it."""
    client = client_as(account)
    page = client.get(reverse("customers:trip", args=[trip.pk])).context["trip"]["total"]
    listing = client.get("/account/trips/", {"tab": "all"}).context
    row = next(r for g in listing["groups"] for r in g["rows"] if r["pk"] == trip.pk)
    table = list(csv.DictReader(io.StringIO(exports.trips_csv(account, trip_ids=[trip.pk]))))
    scoped = _scoped(account, trip)
    receipt = receipts.receipt_context(scoped)["total"] if receipts.has_receipt(scoped) else None
    return {
        "page": page,
        "row": row["total"],
        "foot": listing["total"],
        "csv": Decimal(table[0]["Total"]),
        "receipt": receipt,
    }


def _process(trip: Reservation, collect: str, **kw) -> None:
    """Process the trip through `processing.process` as billing does."""
    trip.refresh_from_db()
    expected = trip_money.money_for(trip).difference
    processing.process(trip, user=UserFactory(), expected=expected, collect=collect, **kw)


def _charge_card(trip, amount, **kw) -> Charge:
    """Stand-in for Stripe's off-session charge: the trip charge succeeds."""
    return _pay(trip, amount, kind=Charge.Kind.TRIP, reservation=trip, at=PROCESSED)


@pytest.mark.parametrize(
    ("collect", "settled", "balance_label"),
    [
        ("card", "120.00", "Balance due"),
        ("link", "120.00", "Balance due"),
        ("keep", "50.00", "Credit"),
        ("invoice", "120.00", None),
    ],
)
def test_a_processed_late_cancel_shows_its_settled_price_everywhere(
    account, client_as, make_trip, settings, collect, settled, balance_label
):
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://example.test"
    on_account = collect == "invoice"
    kw = {"lead__billing_group": AccountGroupFactory()} if on_account else {}
    trip = _booked(account, make_trip, **kw)
    if not on_account:
        _pay(trip, "92.50", at=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _cancel(trip, S.LATE_CANCEL)
    kept = _figures(account, client_as, trip)
    TripExtraFactory(
        reservation=trip,
        label="Late cancellation",
        amount=trip.line_total - Decimal(settled),
        kind="credit",
    )
    trip.refresh_extras_total()

    with (
        patch.object(trip_money, "charge_trip", side_effect=_charge_card),
        patch.object(trip_money, "open_trip_intent"),
        patch("apps.payments.services.send_pay_link"),
    ):
        _process(trip, collect, reason="Kept as credit" if collect == "keep" else "")

    shown = _figures(account, client_as, trip)
    assert set(shown.values()) == {Decimal(settled)}, shown
    assert kept["page"] == kept["row"] == kept["csv"] == Decimal("0.00" if on_account else "92.50")
    text = _text(receipts.render_pdf(receipts.receipt_context(_scoped(account, trip))))
    if balance_label == "Credit":
        assert "Credit" in text and "$42.50" in text
        assert "Balance due" not in text
    elif balance_label:
        assert "Balance due" in text and "Credit" not in text
    else:
        assert "Billed to your account" in text


def test_unprocessed_late_cancel_and_plain_cancellation_show_what_was_kept(
    account, client_as, make_trip
):
    late = _late_cancel(account, make_trip, processed=False)
    plain = _kept_cancel(account, make_trip)

    late_shown = _figures(account, client_as, late)
    plain_shown = _figures(account, client_as, plain)

    assert late_shown["page"] == late_shown["row"] == late_shown["csv"] == Decimal("92.50")
    assert late_shown["receipt"] is None
    assert set(plain_shown.values()) - {plain_shown["foot"]} == {Decimal("92.50")}


def test_a_negative_balance_prints_as_credit(account, make_trip):
    trip = _paid_trip(account, make_trip)
    TripExtraFactory(reservation=trip, label="Goodwill", amount=25, kind="credit")
    trip.refresh_extras_total()

    ctx = receipts.receipt_context(_scoped(account, trip))
    text = _text(receipts.render_pdf(ctx))

    assert ctx["balance_due"] == Decimal("-25.00")
    assert "Credit" in text and "$25.00" in text
    assert "Balance due" not in text
    assert text.count("-$25.00") == 1  # the Goodwill price line, never the balance
