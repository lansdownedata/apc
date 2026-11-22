"""One header for the website and the customer portal, built around the account."""

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.contacts.factories import ContactFactory
from apps.customers.context_processors import who
from apps.customers.factories import CustomerAccountFactory

pytestmark = pytest.mark.django_db


def _header(html: str) -> str:
    return html.partition("</header>")[0]


def _footer(html: str) -> str:
    return html[html.index("<footer") : html.index("</footer>")]


@pytest.fixture
def dana(db):
    return CustomerAccountFactory(
        contact=ContactFactory(name="Dana Mitchell", email="dana@example.com")
    )


# --- who is signed in ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "first", "initials"),
    [
        ("Dana Mitchell", "Dana", "DM"),
        ("Dana", "Dana", "D"),
        ("  mary  ann  o'neil ", "mary", "MO"),
        ("", "Account", "A"),
    ],
)
def test_who_names_the_customer(name, first, initials):
    account = CustomerAccountFactory(contact=ContactFactory(name=name or "x"))
    account.contact.name = name
    person = who(account)
    assert (person["first_name"], person["initials"]) == (first, initials)
    assert person["email"] == account.email


def test_a_visitor_costs_the_website_no_account_query(client):
    with CaptureQueriesContext(connection) as queries:
        html = client.get("/").content.decode()
    assert not [q for q in queries if "customers_customeraccount" in q["sql"]]
    assert "Sign out" not in html


# --- the website -----------------------------------------------------------------------


def test_the_website_offers_sign_in_to_a_visitor(client):
    html = client.get("/").content.decode()
    header = _header(html)
    login = f'href="{reverse("customers:login")}"'
    assert login in header and "Sign in" in header
    assert reverse("customers:signup") in header  # the phone menu's account block
    assert "Customer login" not in html
    assert login in _footer(html) and "Customer sign in" in _footer(html)


@override_settings(CALENDLY_URL="https://calendly.com/allprocharter/quick-chat")
def test_the_website_header_keeps_schedule_a_call(client):
    assert "Schedule a call" in _header(client.get("/").content.decode())


def test_the_website_knows_a_signed_in_customer(client_as, dana):
    html = client_as(dana).get("/").content.decode()
    header = _header(html)
    assert "Dana Mitchell" in header and "dana@example.com" in header
    assert ">DM<" in header
    for name in ("home", "trips", "request_trip", "logout"):
        assert reverse(f"customers:{name}") in header
    assert f'href="{reverse("customers:login")}"' not in html
    assert "My account" in _footer(html)


def test_a_dead_session_reads_as_a_visitor(client_as, dana):
    client = client_as(dana)
    type(dana).objects.filter(pk=dana.pk).delete()
    header = _header(client.get("/").content.decode())
    assert "Dana" not in header and "Sign in" in header


# --- the portal ------------------------------------------------------------------------


def test_the_portal_header_carries_the_account_menu(client_as, dana):
    html = client_as(dana).get(reverse("customers:home")).content.decode()
    header = _header(html)
    assert "Dana Mitchell" in header and "dana@example.com" in header
    assert "Back to website" in header and "Sign out" in header
    assert reverse("customers:account") in header
    assert 'aria-current="page"' in header


def test_the_portal_wears_the_website_footer(client_as, dana):
    html = client_as(dana).get(reverse("customers:home")).content.decode()
    footer = _footer(html)
    assert "bg-charcoal" in footer and "/fleet/" in footer
    # The scheduling panel is not loaded in the portal, so the footer gives the number.
    assert "openScheduler" not in html
    assert 'href="tel:' in footer


@pytest.mark.parametrize("name", ["login", "signup", "password_reset"])
def test_a_signed_out_page_has_the_header_and_a_way_back(name):
    html = Client().get(reverse(f"customers:{name}")).content.decode()
    header = _header(html)
    assert "<header" in header and "Back to website" in header
    assert 'href="tel:' in header
    assert "Sign out" not in html
    assert html.count("apc-logo-color.svg") == 1  # in the header, no longer in the card
    assert "bg-charcoal" in _footer(html)
