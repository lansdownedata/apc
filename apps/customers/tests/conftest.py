"""Fixtures shared by the customer portal's tests."""

from collections.abc import Callable
from importlib import import_module

import pytest
from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory

from apps.accounts.factories import UserFactory
from apps.customers import auth
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.leads.factories import LeadFactory
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation


@pytest.fixture
def rf_with_session(rf: RequestFactory) -> HttpRequest:
    """A bare GET request carrying a fresh session and no `user` attribute."""
    request = rf.get("/account/")
    SessionMiddleware(lambda _: HttpResponse()).process_request(request)
    return request


@pytest.fixture
def account(db) -> CustomerAccount:
    """An active account: registered, password `"correct horse battery"`."""
    return CustomerAccountFactory()


@pytest.fixture
def client_as(db) -> Callable[[CustomerAccount], Client]:
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
def staff_client(client: Client, two_factor_off) -> Client:
    """The `client` fixture signed in as an agent."""
    client.force_login(UserFactory())
    return client


@pytest.fixture
def outbound_on(settings) -> None:
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://example.test"


@pytest.fixture
def make_trip(db) -> Callable[..., Reservation]:
    """`make_trip(account, **kw)` -> a trip on a new lead booked by the account's contact.
    Keyword arguments go to `ReservationFactory`."""

    def _make_trip(account: CustomerAccount, **kw) -> Reservation:
        if "lead" not in kw:
            kw["lead"] = LeadFactory(contact=account.contact)
        return ReservationFactory(**kw)

    return _make_trip
