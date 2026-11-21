"""Home, `/account/`: the next trip, what is waiting on the customer, and what is coming up."""

import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.billing.factories import AccountGroupFactory
from apps.customers import selectors
from apps.customers.factories import CustomerAccountFactory
from apps.leads import services as lead_services
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan
from apps.payments.trip_money import money_for
from apps.reservations.factories import FlightFactory, HourlyReservationFactory
from apps.reservations.models import Reservation, Stop

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
HOME = "/account/"
LA = ZoneInfo("America/Los_Angeles")


def _day(days: int):
    return timezone.localdate() + timedelta(days=days)


def _quote(account, make_trip, days: int = 20, **kw) -> Reservation:
    kw.setdefault("lead__quote_expires_at", timezone.now() + timedelta(days=10))
    return make_trip(
        account,
        lead__status=Lead.Status.QUOTED,
        pickup_date=_day(days),
        pickup_time=time(9, 0),
        **kw,
    )


def _paid_plan(lead: Lead, **kw) -> PaymentPlan:
    """Deposit paid; the balance is the cron's to take from the saved card."""
    return PaymentPlanFactory(
        lead=lead,
        quote_total=lead.quote_total,
        deposit_status=PaymentPlan.DepositStatus.PAID,
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
        stripe_payment_method_id="pm_saved",
        **kw,
    )


def _confirmed(account, make_trip, days: int = 40, **kw) -> Reservation:
    trip = make_trip(
        account,
        lead__status=Lead.Status.BOOKED,
        trip_status=kw.pop("trip_status", S.UNASSIGNED),
        pickup_date=_day(days),
        pickup_time=time(9, 10),
        **kw,
    )
    _paid_plan(trip.lead)
    return trip


def _direct_booking(account, make_trip, days: int = 40) -> Lead:
    """Booked by staff with no payment: the 50% deposit is still owed."""
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(days))
    lead_services.book_lead(trip.lead)
    return trip.lead


def _collect(lead: Lead, amount: Decimal) -> None:
    ledger.post_capture(
        lead=lead,
        amount=amount,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"seed-{lead.pk}-{amount}",
    )


def _page(client) -> str:
    response = client.get(HOME)
    assert response.status_code == 200
    return response.content.decode()


def _trips_route() -> str | None:
    try:
        return reverse("customers:trips")
    except NoReverseMatch:
        return None


# --- layout ----------------------------------------------------------------------------


def test_home_shows_next_trip_then_waiting_then_upcoming(account, client_as, make_trip):
    confirmed = _confirmed(account, make_trip, days=3)
    quote = _quote(account, make_trip, days=20)
    request = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(50))

    data = selectors.home(account)
    assert data["next_trip"]["pk"] == confirmed.pk
    assert [row["pk"] for row in data["waiting"]] == [quote.pk]
    assert [row["pk"] for row in data["upcoming"]] == [confirmed.pk, request.pk]

    body = _page(client_as(account))
    order = [body.index(text) for text in ("Your next trip", "Waiting on you", "Upcoming")]
    assert order == sorted(order)
    assert "Request received" in body
    assert "We&#x27;re preparing your quote." in body


def test_waiting_section_absent_when_empty(account, client_as, make_trip):
    _confirmed(account, make_trip)

    assert selectors.home(account)["waiting"] == []
    assert "Waiting on you" not in _page(client_as(account))


def test_quote_ready_links_to_quote_page_for_that_lead(account, client_as, make_trip):
    one = _quote(account, make_trip, days=30)
    two = _quote(account, make_trip, days=10)
    make_trip(account, lead=two.lead, pickup_date=_day(12))  # a second trip, same quote
    _quote(account, make_trip, lead__quote_expires_at=timezone.now() - timedelta(days=1))
    make_trip(account, lead__status=Lead.Status.ENGAGED)  # their deposit is already held

    waiting = selectors.home(account)["waiting"]

    assert [row["pk"] for row in waiting] == [two.pk, one.pk]  # one row per quote
    for row, trip in zip(waiting, (two, one), strict=True):
        token = lead_services.make_deposit_token(trip.lead)
        assert row["action_url"] == reverse("quote_page", args=[token])
        assert row["action_label"] == "Review and pay"
        assert row["amount"] == trip.lead.quote_total
    client = client_as(account)
    body = _page(client)
    assert "Review and pay" in body
    for row in waiting:
        assert client.get(row["action_url"]).status_code == 200


def test_booked_order_with_money_owed_waits_with_pay_now(account, client_as, make_trip):
    unpaid_deposit = _direct_booking(account, make_trip, days=40)
    failed = _confirmed(account, make_trip, days=20).lead
    failed.payment.balance_status = PaymentPlan.BalanceStatus.FAILED
    failed.payment.save(update_fields=["balance_status"])
    _collect(failed, failed.payment.deposit_amount)

    waiting = selectors.home(account)["waiting"]

    assert [row["lead"] for row in waiting] == [failed.pk, unpaid_deposit.pk]
    assert [row["amount"] for row in waiting] == [
        failed.quote_total - failed.payment.deposit_amount,
        unpaid_deposit.payment.deposit_amount,
    ]
    client = client_as(account)
    assert "Pay now" in _page(client)
    for row, lead in zip(waiting, (failed, unpaid_deposit), strict=True):
        assert row["action_label"] == "Pay now"
        assert row["action_url"] == reverse(
            "quote_pay", args=[lead_services.make_deposit_token(lead)]
        )
        assert client.get(row["action_url"]).status_code == 200


def test_a_trip_pay_link_waits_with_pay_now(account, client_as, make_trip):
    trip = _confirmed(account, make_trip, days=40)
    _collect(trip.lead, trip.lead.payment.deposit_amount)
    owed = money_for(trip).difference
    ChargeFactory(
        plan=trip.lead.payment,
        kind=Charge.Kind.TRIP,
        status=Charge.Status.PENDING,
        amount=owed,
        reservation=trip,
        stripe_client_secret="pi_1_secret",
    )

    [row] = selectors.home(account)["waiting"]

    assert (row["lead"], row["action_label"], row["amount"]) == (trip.lead_id, "Pay now", owed)
    assert client_as(account).get(row["action_url"]).status_code == 200


def test_orders_the_cron_will_collect_are_not_waiting(account, make_trip):
    _confirmed(account, make_trip, days=40)  # balance taken from the card in ten days
    on_account = _direct_booking(account, make_trip)
    on_account.billing_group = AccountGroupFactory()
    on_account.save(update_fields=["billing_group"])
    paid_up = _direct_booking(account, make_trip)
    _collect(paid_up, paid_up.quote_total)

    assert selectors.home(account)["waiting"] == []


# --- the next trip ---------------------------------------------------------------------


def test_next_trip_is_the_soonest_confirmed_or_later(account, make_trip):
    make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(1))
    make_trip(account, lead__status=Lead.Status.ENGAGED, pickup_date=_day(2))
    _quote(account, make_trip, days=3)
    later = _confirmed(account, make_trip, days=30)
    sooner = _confirmed(account, make_trip, days=9, trip_status=S.DISPATCHED)

    assert selectors.home(account)["next_trip"]["pk"] == sooner.pk
    assert selectors.home(account)["next_trip"]["label"] == "Driver assigned"
    sooner.trip_status = S.DONE
    sooner.save(update_fields=["trip_status"])
    assert selectors.home(account)["next_trip"]["pk"] == later.pk


def test_a_confirmed_trip_left_open_after_its_day_is_not_next(account, make_trip):
    stale = _confirmed(account, make_trip, days=-1)
    ahead = _confirmed(account, make_trip, days=5)

    data = selectors.home(account)
    assert data["next_trip"]["pk"] == ahead.pk
    assert [row["pk"] for row in data["upcoming"]] == [stale.pk, ahead.pk]


def test_no_next_trip_before_anything_is_confirmed(account, client_as, make_trip):
    make_trip(account, lead__status=Lead.Status.ENGAGED, pickup_date=_day(2))

    assert selectors.home(account)["next_trip"] is None
    body = _page(client_as(account))
    assert "Your next trip" not in body
    assert "Confirming" in body


def test_next_trip_summary_and_its_own_zone(account, client_as, make_trip):
    lax = AirportFactory(iata="LAX", city="Los Angeles", timezone="America/Los_Angeles")
    trip = _confirmed(
        account,
        make_trip,
        days=12,
        vehicle=VehicleTypeFactory(name="Executive SUV"),
        passengers=2,
        pickup_timezone="America/Los_Angeles",
        stops=[],
    )
    american = AirlineFactory(iata="AA", name="American Airlines")
    flight = FlightFactory(
        airline=american,
        flight_number="2114",
        airport=lax,
        flight_date=trip.pickup_date,
        scheduled_at=datetime.combine(trip.pickup_date, time(8, 52), tzinfo=LA),
    )
    Stop.objects.create(
        reservation=trip,
        sequence=0,
        address="1 World Way",
        airport=lax,
        airline=american,
        flight_number="2114",
        flight=flight,
    )
    Stop.objects.create(reservation=trip, sequence=1, address="1 Main St, Santa Monica, CA")

    next_trip = selectors.home(account)["next_trip"]

    assert next_trip["summary"] == (
        "Executive SUV, 2 passengers. Meeting flight AA 2114, arriving 8:52 AM."
    )
    assert next_trip["zone"] in ("PST", "PDT")  # the pickup's zone, not the viewer's
    body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", _page(client_as(account))))
    assert f"at 9:10 AM {next_trip['zone']}" in body


# --- history banner, money, empty -------------------------------------------------------


def test_history_banner_shows_once(account, client_as, make_trip):
    for days in (-90, -60):
        make_trip(
            account, lead__status=Lead.Status.BOOKED, trip_status=S.DONE, pickup_date=_day(days)
        )
    client = client_as(account)

    assert selectors.home(account)["history_banner"] == 2
    first = _page(client)
    assert "added" in first and "2 past trips" in first
    assert "added" not in _page(client)
    account.refresh_from_db()
    assert account.history_banner_seen_at is not None
    assert selectors.home(account)["history_banner"] is None


def test_no_history_banner_without_past_trips(account, client_as, make_trip):
    _confirmed(account, make_trip)

    assert "added" not in _page(client_as(account))
    account.refresh_from_db()
    assert account.history_banner_seen_at is None


def test_home_has_no_dollar_total(account, client_as, make_trip):
    _confirmed(account, make_trip, days=5)
    _confirmed(account, make_trip, days=-5, trip_status=S.DONE)
    make_trip(account, factory=HourlyReservationFactory, lead__status=Lead.Status.NEW)

    body = _page(client_as(account))
    assert not re.search(r"\$\s*\d", body)  # no amount at all; the shell's `$store` stays
    assert "Trip history and receipts" in body or _trips_route() is None


def test_empty_account_invites_a_first_request(account, client_as):
    body = _page(client_as(account))

    assert "No trips yet" in body
    assert "Request your first trip" in body
    assert "Waiting on you" not in body and "Your next trip" not in body


def test_only_past_trips_says_nothing_is_coming_up(account, client_as, make_trip):
    make_trip(account, lead__status=Lead.Status.BOOKED, trip_status=S.DONE, pickup_date=_day(-9))

    body = _page(client_as(account))
    assert "No trips coming up." in body
    assert "No trips yet" not in body


def test_another_customers_trips_never_show(account, client_as, make_trip):
    theirs = _quote(CustomerAccountFactory(), make_trip)

    data = selectors.home(account)
    assert data["waiting"] == [] and data["upcoming"] == [] and data["next_trip"] is None
    assert theirs.lead.quote_no not in _page(client_as(account))


def test_nav_shows_home_and_history_link_only_with_a_trips_route(account, client_as, make_trip):
    _confirmed(account, make_trip)

    body = _page(client_as(account))
    assert 'aria-current="page"' in body and ">Home</a>" in body
    # Task 6 adds `customers:trips`; the link then appears with no change here.
    assert ("Trip history and receipts" in body) is (_trips_route() is not None)


# --- cost --------------------------------------------------------------------------------


def test_home_query_count(account, client_as, make_trips, django_assert_max_num_queries):
    make_trips(account, 25)
    client = client_as(account)

    with django_assert_max_num_queries(12):
        response = client.get(HOME)
    assert response.status_code == 200
    assert len(response.context["upcoming"]) > 5
