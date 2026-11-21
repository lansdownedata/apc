"""Fixtures shared by the customer portal's tests."""

from collections.abc import Callable
from datetime import time, timedelta
from importlib import import_module

import pytest
from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.factories import UserFactory
from apps.customers import auth
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation


@pytest.fixture
def rf_with_session(rf: RequestFactory) -> HttpRequest:
    """A bare GET request carrying a fresh session and no `user` attribute."""
    request = rf.get("/account/")
    SessionMiddleware(lambda _: HttpResponse()).process_request(request)
    return request


@pytest.fixture
def account(db: None) -> CustomerAccount:
    """An active account: registered, password `"correct horse battery"`."""
    return CustomerAccountFactory()


@pytest.fixture
def client_as(db: None) -> Callable[[CustomerAccount], Client]:
    """`client_as(account)` -> a new test client signed in through `auth.sign_in`."""

    def _client_as(account: CustomerAccount) -> Client:
        client = Client()
        request = HttpRequest()
        request.session = import_module(settings.SESSION_ENGINE).SessionStore()
        auth.sign_in(request, account)
        request.session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
        return client

    return _client_as


@pytest.fixture
def staff_client(db: None, two_factor_off: None) -> Client:
    """A test client of its own, signed in as an agent."""
    client = Client()
    client.force_login(UserFactory())
    return client


@pytest.fixture
def outbound_on(settings: Settings) -> None:
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://example.test"


@pytest.fixture
def make_trip(db: None) -> Callable[..., Reservation]:
    """`make_trip(account, **kw)` -> a trip on a new lead booked by the account's contact.
    `lead__<field>` keywords go to that new lead, `factory` picks a `ReservationFactory`
    subclass, and the rest go to the trip's factory."""

    def _make_trip(
        account: CustomerAccount,
        factory: type[ReservationFactory] = ReservationFactory,
        **kw: object,
    ) -> Reservation:
        lead_kw = {k.removeprefix("lead__"): kw.pop(k) for k in list(kw) if k.startswith("lead__")}
        if "lead" not in kw:
            kw["lead"] = LeadFactory(**{"contact": account.contact, **lead_kw})
        return factory(**kw)

    return _make_trip


# What `make_trips` cycles through: (lead status, trip status, days from today, plan).
# "paid" is a deposit-paid order whose balance the cron will take from the saved card.
_MIX = (
    (Lead.Status.BOOKED, Reservation.TripStatus.UNASSIGNED, 40, "paid"),
    (Lead.Status.BOOKED, Reservation.TripStatus.DISPATCHED, 45, "paid"),
    (Lead.Status.BOOKED, Reservation.TripStatus.DONE, -30, "paid"),
    (Lead.Status.BOOKED, Reservation.TripStatus.CANCELLED, -20, "paid"),
    (Lead.Status.NEW, Reservation.TripStatus.PENDING, 50, None),
    (Lead.Status.QUOTED, Reservation.TripStatus.PENDING, 55, None),
    (Lead.Status.ENGAGED, Reservation.TripStatus.PENDING, 60, None),
)


@pytest.fixture
def make_trips(make_trip: Callable[..., Reservation]) -> Callable[..., list[Reservation]]:
    """`make_trips(account, n)` -> `n` trips, each on its own lead, cycling through the
    states Home shows (`_MIX`): booked and paid, completed, cancelled, a request, a quote
    ready to pay and a held deposit. None of them owes money now."""

    def _make_trips(account: CustomerAccount, n: int) -> list[Reservation]:
        today = timezone.localdate()
        trips = []
        for i in range(n):
            lead_status, trip_status, days, plan = _MIX[i % len(_MIX)]
            trip = make_trip(
                account,
                lead__status=lead_status,
                lead__quote_expires_at=timezone.now() + timedelta(days=30),
                trip_status=trip_status,
                pickup_date=today + timedelta(days=days + i),
                pickup_time=time(9, 0),
            )
            if plan == "paid":
                PaymentPlanFactory(
                    lead=trip.lead,
                    quote_total=trip.line_total,
                    deposit_status=PaymentPlan.DepositStatus.PAID,
                    balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
                    stripe_payment_method_id="pm_saved",
                )
            trips.append(trip)
        return trips

    return _make_trips
