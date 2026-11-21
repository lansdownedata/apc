"""The customer account, its session and its signed links: kept apart from staff sign-in."""

from urllib.parse import quote

import pytest
from django.contrib.auth import get_user
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.urls import include, path, resolve, reverse
from django.utils import timezone

from apps.customers import auth
from apps.customers.auth import HASH_SESSION_KEY, SESSION_KEY, customer_required
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.customers.tokens import customer_reset_token, customer_token

pytestmark = pytest.mark.django_db

PROBE = "/probe/?tab=past"


@customer_required
def _probe(request: HttpRequest) -> HttpResponse:
    return HttpResponse(f"customer {request.customer.pk}")


# The guard is tried on a stand-in page here; the portal's own pages come in later tasks.
urlpatterns = [
    path("account/", include("apps.customers.urls")),
    path("probe/", _probe),
]
with_probe = pytest.mark.urls(__name__)


def _login_redirect(next_path: str) -> str:
    return f"{reverse('customers:login')}?next={quote(next_path, safe='/')}"


# --- the account -------------------------------------------------------------------


def test_email_is_the_contacts_live_email():
    account = CustomerAccountFactory(contact__email="Dana@Example.com")
    assert account.email == "dana@example.com"
    account.contact.email = "new@example.com"
    account.contact.save(update_fields=["email"])
    assert CustomerAccount.objects.get(pk=account.pk).email == "new@example.com"


def test_blank_email_cannot_sign_in():
    account = CustomerAccountFactory(contact__email=None)
    assert account.email == ""
    assert account.status == CustomerAccount.Status.ACTIVE
    assert account.can_sign_in is False


def test_status_is_derived_and_revoked_wins():
    assert CustomerAccountFactory(invited=True).status == CustomerAccount.Status.INVITED
    active = CustomerAccountFactory()
    assert active.status == CustomerAccount.Status.ACTIVE
    assert active.can_sign_in is True
    revoked = CustomerAccountFactory(revoked=True)
    assert revoked.status == CustomerAccount.Status.REVOKED
    assert revoked.can_sign_in is False


def test_an_invited_account_cannot_sign_in():
    account = CustomerAccountFactory(invited=True)
    assert account.has_usable_password() is False
    assert account.can_sign_in is False


def test_factory_password_checks():
    account = CustomerAccountFactory()
    assert account.check_password("correct horse battery")
    assert not account.check_password("wrong")


# --- the session ---------------------------------------------------------------------


def test_sign_in_sets_only_customer_keys(rf_with_session):
    account = CustomerAccountFactory()
    auth.sign_in(rf_with_session, account)
    assert set(rf_with_session.session.keys()) == {SESSION_KEY, HASH_SESSION_KEY}
    assert "_auth_user_id" not in rf_with_session.session
    assert not hasattr(rf_with_session, "user")
    account.refresh_from_db()
    assert account.last_login is not None
    assert auth.current_customer(rf_with_session) == account


def test_sign_out_drops_the_customer_keys(rf_with_session):
    account = CustomerAccountFactory()
    auth.sign_in(rf_with_session, account)
    auth.sign_out(rf_with_session)
    assert SESSION_KEY not in rf_with_session.session
    assert HASH_SESSION_KEY not in rf_with_session.session
    assert auth.current_customer(rf_with_session) is None


def test_customer_required_never_touches_request_user(rf_with_session):
    account = CustomerAccountFactory()
    auth.sign_in(rf_with_session, account)
    # No `user` attribute at all: reading it would raise, setting it would show up.
    response = _probe(rf_with_session)
    assert response.status_code == 200
    assert rf_with_session.customer == account
    assert not hasattr(rf_with_session, "user")


@with_probe
def test_signed_in_customer_gets_through_and_the_visit_is_logged(client_as, account):
    response = client_as(account).get(PROBE)
    assert response.status_code == 200
    assert response.content == f"customer {account.pk}".encode()
    account.refresh_from_db()
    assert account.last_seen_at is not None


@with_probe
def test_anonymous_visitor_is_sent_to_sign_in_with_next(client):
    response = client.get(PROBE)
    assert response.status_code == 302
    assert response["Location"] == _login_redirect(PROBE)


@with_probe
def test_revoked_account_is_signed_out_on_next_request(client_as, account):
    client = client_as(account)
    assert client.get(PROBE).status_code == 200
    account.revoked_at = timezone.now()
    account.save(update_fields=["revoked_at"])
    response = client.get(PROBE)
    assert response["Location"] == _login_redirect(PROBE)
    assert SESSION_KEY not in client.session
    # Restoring access later does not revive the old session.
    account.revoked_at = None
    account.save(update_fields=["revoked_at"])
    assert client.get(PROBE).status_code == 302


@with_probe
def test_cleared_contact_email_ends_the_session(client_as, account):
    client = client_as(account)
    account.contact.email = None
    account.contact.save(update_fields=["email"])
    assert client.get(PROBE)["Location"] == _login_redirect(PROBE)


@with_probe
def test_password_change_ends_other_sessions(client_as, account):
    elsewhere = client_as(account)
    assert elsewhere.get(PROBE).status_code == 200
    account.set_password("a brand new pass phrase")
    account.save(update_fields=["password"])
    assert elsewhere.get(PROBE)["Location"] == _login_redirect(PROBE)
    assert client_as(account).get(PROBE).status_code == 200


@with_probe
def test_staff_session_does_not_pass_customer_required(staff_client):
    response = staff_client.get(PROBE)
    assert response.status_code == 302
    assert response["Location"] == _login_redirect(PROBE)


def test_account_url_resolves_to_this_app():
    assert reverse("customers:login") == "/account/login/"
    assert resolve("/account/login/").namespace == "customers"


def test_make_trip_books_on_the_accounts_contact(account, make_trip):
    trip = make_trip(account, passengers=4)
    assert trip.lead.contact == account.contact
    assert trip.passengers == 4


def test_customer_session_is_anonymous_to_staff_auth(client_as, account):
    client = client_as(account)
    assert "_auth_user_id" not in client.session
    request = RequestFactory().get("/")
    request.session = client.session
    assert get_user(request).is_authenticated is False


# --- signed links --------------------------------------------------------------------


def test_token_dies_when_contact_email_changes():
    account = CustomerAccountFactory(invited=True)
    invite = customer_token.make_token(account)
    reset = customer_reset_token.make_token(account)
    assert customer_token.check_token(account, invite)
    account.contact.email = "someone-else@example.com"
    account.contact.save(update_fields=["email"])
    account = CustomerAccount.objects.select_related("contact").get(pk=account.pk)
    assert customer_token.check_token(account, invite) is False
    assert customer_reset_token.check_token(account, reset) is False


def test_token_dies_once_a_password_is_set():
    account = CustomerAccountFactory(invited=True)
    invite = customer_token.make_token(account)
    account.set_password("a long pass phrase")
    account.registered_at = timezone.now()
    account.save(update_fields=["password", "registered_at"])
    assert customer_token.check_token(account, invite) is False


def test_reset_token_cannot_be_used_as_invite_token():
    account = CustomerAccountFactory()
    reset = customer_reset_token.make_token(account)
    invite = customer_token.make_token(account)
    assert customer_reset_token.check_token(account, reset)
    assert customer_token.check_token(account, reset) is False
    assert customer_reset_token.check_token(account, invite) is False
