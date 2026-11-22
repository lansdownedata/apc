"""Trips and history, `/account/trips/`: tabs, dates, search, grouping, the foot total and
the customer's own "Your reference"."""

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.contrib.contenttypes.models import ContentType
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEntry
from apps.customers import requests, selectors
from apps.customers.factories import CustomerAccountFactory
from apps.customers.selectors import TripFilter
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import PassengerFactory, ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
TRIPS = "/account/trips/"
NY = ZoneInfo("America/New_York")


def _day(days: int) -> date:
    return timezone.localdate() + timedelta(days=days)


def _done(account, make_trip, day: date, **kw) -> Reservation:
    """A completed trip on a booked order."""
    kw.setdefault("pickup_time", time(9, 0))
    return make_trip(
        account, lead__status=Lead.Status.BOOKED, trip_status=S.DONE, pickup_date=day, **kw
    )


def _confirmed(account, make_trip, day: date, **kw) -> Reservation:
    return make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        trip_status=S.UNASSIGNED,
        pickup_date=day,
        pickup_time=time(9, 0),
        **kw,
    )


def _request(account, make_trip, day: date, **kw) -> Reservation:
    """An unpriced request: a New lead nobody has quoted yet."""
    kw.setdefault("rate", 0)
    return make_trip(account, lead__status=Lead.Status.NEW, pickup_date=day, **kw)


def pks(result: dict) -> list[int]:
    return [row["pk"] for group in result["groups"] for row in group["rows"]]


def _trips(account, **kw) -> dict:
    return selectors.trips(account, TripFilter(**kw))


def _get(client, **params) -> str:
    response = client.get(TRIPS, params)
    assert response.status_code == 200
    return response.content.decode()


# --- the filter --------------------------------------------------------------------------


def test_filter_defaults():
    f = TripFilter.from_query(QueryDict(""))
    assert f == TripFilter(
        tab="upcoming", start=None, end=None, q="", passenger="", group="month", page=1
    )


def test_filter_reads_the_query_string():
    f = TripFilter.from_query(
        QueryDict(
            "tab=past&start=2026-10-01&end=2026-11-20&q=+Board+&passenger=Dana&group=reference"
            "&page=3"
        )
    )
    assert f == TripFilter(
        tab="past",
        start=date(2026, 10, 1),
        end=date(2026, 11, 20),
        q="Board",
        passenger="Dana",
        group="reference",
        page=3,
    )


def test_a_backwards_range_is_read_the_right_way_round():
    f = TripFilter.from_query(QueryDict("start=2026-11-20&end=2026-10-01"))
    assert (f.start, f.end) == (date(2026, 10, 1), date(2026, 11, 20))


def test_bad_filter_values_fall_back_to_defaults(account, client_as, make_trip):
    _confirmed(account, make_trip, _day(5))
    f = TripFilter.from_query(QueryDict("tab=x&start=nope&end=2026-13-45&group=zone&page=-3"))
    assert f == TripFilter()

    response = client_as(account).get(TRIPS, {"tab": "x", "start": "nope", "page": "-3"})
    assert response.status_code == 200
    assert response.context["filter"]["tab"] == "upcoming"
    assert len(pks(response.context)) == 1


# --- tabs and dates ----------------------------------------------------------------------


def test_tabs_split_upcoming_past_requests(account, client_as, make_trip):
    ahead = _confirmed(account, make_trip, _day(5))
    done = _done(account, make_trip, _day(-5))
    asked = _request(account, make_trip, _day(30))
    client = client_as(account)

    def shown(tab: str) -> set[int]:
        response = client.get(TRIPS, {"tab": tab})
        assert response.status_code == 200
        return set(pks(response.context))

    assert shown("upcoming") == {ahead.pk}
    assert shown("past") == {done.pk}
    assert shown("requests") == {asked.pk}
    assert shown("all") == {ahead.pk, done.pk, asked.pk}
    assert set(pks(client.get(TRIPS).context)) == {ahead.pk}  # Upcoming by default


def test_upcoming_runs_soonest_first_and_past_newest_first(account, make_trip):
    later, sooner = _confirmed(account, make_trip, _day(9)), _confirmed(account, make_trip, _day(2))
    older, newer = _done(account, make_trip, _day(-9)), _done(account, make_trip, _day(-2))

    assert pks(_trips(account, tab="upcoming")) == [sooner.pk, later.pk]
    assert pks(_trips(account, tab="past")) == [newer.pk, older.pk]


def test_late_evening_trip_is_inside_its_local_day(account, make_trip):
    trip = _done(
        account,
        make_trip,
        date(2026, 11, 30),
        pickup_time=time(23, 30),
        pickup_timezone="America/New_York",
    )
    assert trip.pickup_at.astimezone(ZoneInfo("UTC")).date() == date(2026, 12, 1)

    f = TripFilter(tab="past", start=date(2026, 11, 1), end=date(2026, 11, 30))
    assert trip.pk in pks(selectors.trips(account, f))
    f = TripFilter(tab="past", start=date(2026, 12, 1), end=date(2026, 12, 31))
    assert trip.pk not in pks(selectors.trips(account, f))


def test_date_range_is_inclusive_and_labelled(account, make_trip):
    first = _done(account, make_trip, date(2026, 10, 1))
    last = _done(account, make_trip, date(2026, 11, 20))
    _done(account, make_trip, date(2026, 9, 30))
    _done(account, make_trip, date(2026, 11, 21))

    result = _trips(account, tab="past", start=date(2026, 10, 1), end=date(2026, 11, 20))
    assert set(pks(result)) == {first.pk, last.pk}
    assert result["count"] == 2
    assert result["range_label"] == "from Oct 1 to Nov 20, 2026"
    assert result["range_text"] == "Oct 1 – Nov 20, 2026"
    across = _trips(account, tab="past", start=date(2025, 12, 1), end=date(2026, 1, 31))
    assert across["range_label"] == "from Dec 1, 2025 to Jan 31, 2026"
    assert _trips(account, tab="past")["range_label"] == ""


def test_quick_ranges_use_the_business_day():
    now = datetime(2026, 11, 20, 15, 0, tzinfo=ZoneInfo("UTC"))
    with patch("django.utils.timezone.now", return_value=now):
        ranges = selectors.quick_ranges()

    assert [(r["label"], r["start"], r["end"]) for r in ranges] == [
        ("This month", "2026-11-01", "2026-11-30"),
        ("Last month", "2026-10-01", "2026-10-31"),
        ("This quarter", "2026-10-01", "2026-12-31"),
        ("2026", "2026-01-01", "2026-12-31"),
        ("2025", "2025-01-01", "2025-12-31"),
    ]


# --- search ------------------------------------------------------------------------------


def test_search_by_reference_passenger_address_and_confirmation(account, make_trip):
    tagged = _done(account, make_trip, _day(-3), customer_reference="Board offsite")
    rider = _done(account, make_trip, _day(-4))
    PassengerFactory(reservation=rider, name="Dana Whitfield", is_lead=True)
    street = _done(
        account, make_trip, _day(-5), stops=["1100 Pennsylvania Ave NW, Washington, DC 20004"]
    )
    imported = _done(account, make_trip, _day(-6), la_confirmation="48213")
    plain = _done(account, make_trip, _day(-7))

    def found(q: str) -> list[int]:
        return pks(_trips(account, tab="all", q=q))

    assert found("board OFFSITE") == [tagged.pk]
    assert found("whitfield") == [rider.pk]
    assert found("Pennsylvania") == [street.pk]
    assert found("48213") == [imported.pk]
    assert found(plain.reference) == [plain.pk]
    assert found(plain.reference.lower()) == [plain.pk]
    assert found(str(600_000 + plain.pk)) == [plain.pk]


@pytest.mark.parametrize(
    "q", ["APC-", "APC-abc", "APC-600000", "APC-12", "APC-99999999999999999999999", "APC--1"]
)
def test_a_reference_that_is_not_a_trip_matches_nothing_and_never_raises(account, make_trip, q):
    _done(account, make_trip, _day(-3))
    assert pks(_trips(account, tab="all", q=q)) == []


def test_a_trip_with_two_matching_stops_appears_once(account, make_trip):
    trip = _done(account, make_trip, _day(-3), stops=["12 Elm St, Reston, VA", "40 Elm St, Reston"])
    PassengerFactory(reservation=trip, name="Elm Parker")

    result = _trips(account, tab="all", q="Elm")
    assert pks(result) == [trip.pk]
    assert result["count"] == 1


def test_search_stays_inside_the_account(account, make_trip):
    other = CustomerAccountFactory()
    theirs = _done(other, make_trip, _day(-3), customer_reference="Board offsite")
    mine = _done(account, make_trip, _day(-3), customer_reference="Board offsite")

    assert pks(_trips(account, tab="all", q="Board")) == [mine.pk]
    assert pks(_trips(account, tab="all", q=theirs.reference)) == []


def test_passenger_filter_and_its_options(account, make_trip):
    dana = _done(account, make_trip, _day(-3))
    PassengerFactory(reservation=dana, name="Dana Whitfield", is_lead=True)
    PassengerFactory(reservation=dana, name="Priya Raman")
    priya = _done(account, make_trip, _day(-4))
    PassengerFactory(reservation=priya, name="Priya Raman", is_lead=True)
    PassengerFactory(reservation=_done(CustomerAccountFactory(), make_trip, _day(-3)), name="Zed")

    result = _trips(account, tab="all", passenger="priya raman")
    assert set(pks(result)) == {dana.pk, priya.pk}
    assert result["passengers"] == [
        ("Dana Whitfield", "Dana Whitfield"),
        ("Priya Raman", "Priya Raman"),
    ]


# --- grouping ----------------------------------------------------------------------------


def test_group_by_month_in_the_trips_own_order(account, make_trip):
    nov = _done(account, make_trip, date(2026, 11, 18))
    oct_ = _done(account, make_trip, date(2026, 10, 27))

    groups = _trips(account, tab="past")["groups"]
    assert [(g["title"], [r["pk"] for r in g["rows"]]) for g in groups] == [
        ("November 2026", [nov.pk]),
        ("October 2026", [oct_.pk]),
    ]


def test_group_by_reference_puts_blank_last_under_no_reference(account, make_trip):
    blank = _done(account, make_trip, _day(-1))
    board = _done(account, make_trip, _day(-2), customer_reference="Board offsite")
    halvorsen = _done(account, make_trip, _day(-3), customer_reference="Halvorsen engagement")
    board_too = _done(account, make_trip, _day(-4), customer_reference="Board offsite")

    groups = _trips(account, tab="past", group="reference")["groups"]
    assert [(g["title"], [r["pk"] for r in g["rows"]]) for g in groups] == [
        ("Board offsite", [board.pk, board_too.pk]),
        ("Halvorsen engagement", [halvorsen.pk]),
        ("No reference", [blank.pk]),
    ]


def test_group_by_passenger_puts_unnamed_last(account, make_trip):
    unnamed = _done(account, make_trip, _day(-1))
    priya = _done(account, make_trip, _day(-2))
    PassengerFactory(reservation=priya, name="Priya Raman", is_lead=True)

    groups = _trips(account, tab="past", group="passenger")["groups"]
    assert [(g["title"], [r["pk"] for r in g["rows"]]) for g in groups] == [
        ("Priya Raman", [priya.pk]),
        ("No passenger named", [unnamed.pk]),
    ]


# --- money -------------------------------------------------------------------------------


def test_total_covers_whole_filter_not_just_first_page(account, make_trip):
    for i in range(60):
        _done(account, make_trip, _day(-1 - i), rate=100)

    first = _trips(account, tab="past")
    assert len(pks(first)) == selectors.PAGE_SIZE == 50
    assert first["has_more"] is True
    assert first["count"] == 60
    assert first["total"] == Decimal("6000.00")

    second = _trips(account, tab="past", page=2)
    assert len(pks(second)) == 60 and second["has_more"] is False
    assert second["total"] == Decimal("6000.00")


def test_total_ignores_quotes_and_unpriced_requests(account, make_trip):
    done = _done(account, make_trip, _day(-3), rate=100)
    lead = LeadFactory(contact=account.contact, status=Lead.Status.BOOKED)
    plan = PaymentPlanFactory(lead=lead)
    ChargeFactory(plan=plan, amount=Decimal("40.00"), status=Charge.Status.SUCCEEDED)
    ReservationFactory(
        lead=lead,
        rate=185,
        booked_total=Decimal("185.00"),
        trip_status=S.LATE_CANCEL,
        pickup_date=_day(-2),
    )
    make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        lead__quote_expires_at=timezone.now() + timedelta(days=9),
        pickup_date=_day(20),
    )
    _request(account, make_trip, _day(30))
    _confirmed(account, make_trip, _day(10))

    result = _trips(account, tab="all")
    assert result["count"] == 5
    assert result["total"] == done.line_total + Decimal("40.00")
    assert result["counted"] == 2


def test_unpriced_request_shows_dash(account, client_as, make_trip):
    asked = _request(account, make_trip, _day(30))

    body = _get(client_as(account), tab="requests")
    row = re.search(rf'data-trip="{asked.pk}".*?</tr>', body, re.S).group(0)
    assert "Request received" in row
    assert "—" in row and not re.search(r"\$\s*\d", row)


def test_a_draft_rate_on_a_new_lead_shows_no_amount_until_the_quote_is_sent(
    account, client_as, make_trip
):
    _done(account, make_trip, _day(-3), rate=100)
    draft = _request(account, make_trip, _day(30), rate=185)

    result = _trips(account, tab="all")
    row = next(r for g in result["groups"] for r in g["rows"] if r["pk"] == draft.pk)
    assert row["total"] is None
    assert result["total"] == Decimal("100.00")
    body = _get(client_as(account), tab="all")
    html_row = re.search(rf'data-trip="{draft.pk}".*?</tr>', body, re.S).group(0)
    assert "—" in html_row and "$185.00" not in body
    assert f'data-pick="{draft.pk}" data-amount="0.00"' in body

    draft.lead.status = Lead.Status.QUOTED
    draft.lead.quote_expires_at = timezone.now() + timedelta(days=9)
    draft.lead.save(update_fields=["status", "quote_expires_at"])
    assert "$185.00" in _get(client_as(account), tab="all")


def test_page_shows_header_count_rows_and_one_plain_total(account, client_as, make_trip):
    _done(account, make_trip, date(2026, 11, 18), rate=100, customer_reference="Board offsite")
    _done(account, make_trip, date(2026, 10, 27), rate=100)

    body = _get(client_as(account), tab="past", start="2026-10-01", end="2026-11-20")
    assert "2 trips" in body and "from Oct 1 to Nov 20, 2026" in body
    assert "November 2026" in body and "October 2026" in body
    assert "Board offsite" in body and "Add reference" in body
    assert body.count("Total for these 2 trips") == 1
    assert "$200.00" in body
    assert "noindex" in body


def test_fragment_is_the_table_alone(account, client_as, make_trip):
    for i in range(51):
        _done(account, make_trip, _day(-1 - i), rate=100)

    body = _get(client_as(account), tab="past", fragment="1")
    assert "<html" not in body and "Sign out" not in body
    assert "51 trips" in body and "Show more" in body
    body = _get(client_as(account), tab="past", fragment="1", page="2")
    assert "Show more" not in body


def test_empty_account_invites_a_first_request(account, client_as):
    body = _get(client_as(account))
    assert "No trips yet" in body
    assert "Clear filters" not in body


def test_no_match_offers_clear_filters(account, client_as, make_trip):
    _done(account, make_trip, _day(-3))

    body = _get(client_as(account), tab="past", q="nothing like this")
    assert "No trips match these filters." in body
    assert "Clear filters" in body
    assert "No trips yet" not in body


def test_another_customers_trips_never_show(account, client_as, make_trip):
    theirs = _done(CustomerAccountFactory(), make_trip, _day(-3), customer_reference="Secret")

    body = _get(client_as(account), tab="all")
    assert theirs.reference not in body and "Secret" not in body


def test_signed_out_visitor_goes_to_sign_in(client):
    response = client.get(TRIPS)
    assert response.status_code == 302
    assert response.url.startswith(reverse("customers:login"))


def test_trips_query_count_with_50_rows(
    account, client_as, make_trips, make_trip, django_assert_max_num_queries
):
    trips = make_trips(account, 50)
    for i in range(3):
        _request(account, make_trip, _day(70 + i))
    cancelled_orders = sum(1 for t in trips if t.trip_status == S.CANCELLED)
    assert cancelled_orders >= 3
    client = client_as(account)

    with django_assert_max_num_queries(BASE_QUERIES + PER_CANCELLED_ORDER * cancelled_orders):
        response = client.get(TRIPS, {"tab": "all"})
    assert response.status_code == 200
    assert len(pks(response.context)) == 50
    assert response.context["count"] == 53


# Session, account, last seen, the trips with their stops and passengers, the cancelled
# trips' own charges and the passenger options (measured: 8).
BASE_QUERIES = 8
# `trip_money.allocation` per order holding a cancelled trip: two prepaid sums, frozen
# shares, the order's trips.
PER_CANCELLED_ORDER = 4


# --- Your reference ----------------------------------------------------------------------


def test_trip_row_carries_the_customer_reference(account, make_trip):
    trip = _done(account, make_trip, _day(-3), customer_reference="Board offsite")
    assert selectors.trip_row(trip)["reference"] == "Board offsite"


def test_set_reference_trims_caps_and_counts_changes(account, make_trip):
    a = _done(account, make_trip, _day(-3))
    b = _done(account, make_trip, _day(-4), customer_reference="Board offsite")

    assert requests.set_reference(account, [a.pk, b.pk], "  Board offsite  ") == 1
    a.refresh_from_db()
    assert a.customer_reference == "Board offsite"
    assert requests.set_reference(account, [a.pk], "x" * 200) == 1
    a.refresh_from_db()
    assert a.customer_reference == "x" * 120
    assert requests.set_reference(account, [a.pk, b.pk], "") == 2
    b.refresh_from_db()
    assert b.customer_reference == ""


def test_set_reference_ignores_other_customers_ids(account, make_trip):
    mine = _done(account, make_trip, _day(-3))
    theirs = _done(CustomerAccountFactory(), make_trip, _day(-3))

    assert requests.set_reference(account, [mine.pk, theirs.pk], "Board offsite") == 1
    theirs.refresh_from_db()
    assert theirs.customer_reference == ""
    mine.refresh_from_db()
    assert mine.customer_reference == "Board offsite"


def test_set_reference_writes_an_audit_entry_with_portal_source(account, client_as, make_trip):
    trip = _done(account, make_trip, _day(-3))
    theirs = _done(CustomerAccountFactory(), make_trip, _day(-3))
    reservations = ContentType.objects.get_for_model(Reservation)
    before = AuditEntry.objects.filter(content_type=reservations, object_id=theirs.pk).count()

    response = client_as(account).post(
        reverse("customers:set_reference"),
        {"ids": f"{trip.pk},{theirs.pk},nope", "value": "Board offsite"},
    )

    assert response.status_code == 200
    assert json.loads(response.content) == {"ok": True, "changed": 1}
    entry = AuditEntry.objects.filter(content_type=reservations, object_id=trip.pk).latest("pk")
    assert entry.source == "customer portal"
    assert entry.actor is None and entry.actor_email == account.email
    assert entry.changes["customer_reference"] == ["", "Board offsite"]
    after = AuditEntry.objects.filter(content_type=reservations, object_id=theirs.pk).count()
    assert after == before


def test_set_reference_accepts_repeated_ids(account, client_as, make_trip):
    a, b = _done(account, make_trip, _day(-3)), _done(account, make_trip, _day(-4))

    response = client_as(account).post(
        reverse("customers:set_reference"), {"ids": [a.pk, b.pk], "value": "Offsite"}
    )
    assert json.loads(response.content) == {"ok": True, "changed": 2}


def test_set_reference_is_post_only_and_signed_in_only(account, client_as, client):
    assert client_as(account).get(reverse("customers:set_reference")).status_code == 405
    response = client.post(reverse("customers:set_reference"), {"ids": "1", "value": "x"})
    assert response.status_code == 302


def test_reference_is_not_shared_across_a_linked_set():
    from apps.reservations import groups

    assert "customer_reference" not in groups.propagated_fields()


def test_audit_labels_the_field():
    from apps.audit import registry

    assert registry.field_label(Reservation, "customer_reference") == "Customer reference"


# --- staff side and the shell ------------------------------------------------------------


def test_staff_trip_line_shows_the_customer_reference(staff_client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, customer_reference="Board offsite")
    ReservationFactory(lead=lead)

    body = staff_client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert body.count("data-customer-reference") == 1
    assert "Board offsite" in body


def test_home_and_nav_link_to_trips(account, client_as, make_trip):
    _done(account, make_trip, _day(-3))
    _confirmed(account, make_trip, _day(3))
    client = client_as(account)

    home = client.get(reverse("customers:home")).content.decode()
    assert f'href="{TRIPS}"' in home  # nav and "All trips"
    assert f'href="{TRIPS}?tab=past"' in home  # "Trip history and receipts"
    assert "Trip history and receipts" in home
    trips = _get(client)
    assert re.search(rf'href="{TRIPS}"\s+aria-current="page"', trips)


# --- a session that ended mid-page (review fix round 1) ------------------------------------

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@pytest.mark.parametrize(
    "headers", [AJAX, {"HTTP_ACCEPT": "application/json"}], ids=["xhr", "accept-json"]
)
def test_signed_out_reference_save_is_401_and_writes_nothing(client, make_trip, account, headers):
    trip = _done(account, make_trip, _day(-3))

    response = client.post(
        reverse("customers:set_reference"),
        {"ids": str(trip.pk), "value": "Board offsite"},
        **headers,
    )

    assert response.status_code == 401
    assert json.loads(response.content) == {"ok": False, "error": "signed_out"}
    trip.refresh_from_db()
    assert trip.customer_reference == ""


def test_signed_out_fragment_fetch_is_401(client):
    response = client.get(TRIPS, {"tab": "past", "fragment": "1"}, **AJAX)
    assert response.status_code == 401


def test_signed_out_page_load_returns_to_the_page_never_the_fragment(client):
    response = client.get(TRIPS, {"tab": "past", "q": "Board", "fragment": "1"})

    assert response.status_code == 302
    sent_to = urlsplit(response.url)
    assert sent_to.path == reverse("customers:login")
    nxt = QueryDict(sent_to.query)["next"]
    assert urlsplit(nxt).path == TRIPS
    assert QueryDict(urlsplit(nxt).query).dict() == {"tab": "past", "q": "Board"}


def test_sign_in_never_lands_on_a_fragment(client, account):
    response = client.post(
        reverse("customers:login") + "?" + urlencode({"next": f"{TRIPS}?tab=past&fragment=1"}),
        {"email": account.email, "password": "correct horse battery"},
    )
    assert response.status_code == 302
    assert response.url == f"{TRIPS}?tab=past"


# --- confirmation numbers (review fix round 1) ---------------------------------------------


def test_an_imported_trip_reads_conf_and_ours_reads_its_reference(account, client_as, make_trip):
    imported = _done(account, make_trip, _day(-3), la_confirmation="48213")
    ours = _done(account, make_trip, _day(-4))

    rows = {r["pk"]: r for r in pks_rows(_trips(account, tab="past"))}
    assert rows[imported.pk]["confirmation"] == "48213"  # raw, for search and export
    assert rows[imported.pk]["confirmation_label"] == "Conf. 48213"
    assert rows[ours.pk]["confirmation_label"] == ours.reference

    body = _get(client_as(account), tab="past")
    row = re.search(rf'data-trip="{imported.pk}".*?</tr>', body, re.S).group(0)
    assert "Conf. 48213" in row
    row = re.search(rf'data-trip="{ours.pk}".*?</tr>', body, re.S).group(0)
    assert ours.reference in row and "Conf." not in row


def pks_rows(result: dict) -> list[dict]:
    return [row for group in result["groups"] for row in group["rows"]]
