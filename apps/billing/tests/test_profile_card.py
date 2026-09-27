"""The Billing card on a customer's profile.

Two tabs — the cards they have paid with, and the terms accounts they can be invoiced on.
Built to the mockup Moe approved at
`docs/superpowers/specs/2026-09-19-billing-accounts/customer-profile-billing.html`.

The money figures are real fields returning zero, not placeholders: invoices arrive in
Phase 4, and a selector that already has the right shape is the difference between filling
in a number then and rebuilding the card then.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.billing import services
from apps.billing.models import SyncState, Terms
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    """Someone who may act on what the card shows.

    Reading it is open to every agent, but the controls on it are not — changing terms or
    who gets the invoice is a money decision, gated the same way the endpoints are
    (`test_account_views.py`). This file is about what the card renders, so its viewer is
    the one who sees all of it.
    """
    client.force_login(UserFactory(can_manage_payments=True))


def _account(contact=None, **over):
    fields = {
        "name": "Ridgeline Partners",
        "terms": Terms.NET_30,
        "group_name": "Accounts Payable",
        "invoice_email": "ap@ridgeline.example",
    }
    return services.create_account(contact or ContactFactory(), **{**fields, **over})


def _profile(client, contact) -> str:
    return client.get(reverse("contact_detail", args=[contact.pk])).content.decode()


# --- the card is there, and where it should be ---------------------------------------


def test_the_profile_carries_a_billing_card(client, agent):
    body = _profile(client, ContactFactory())
    assert "Billing accounts" in body
    assert "Cards" in body


def test_billing_sits_above_order_history(client, agent):
    """Money the office can act on comes before the archive of what already happened."""
    from apps.leads.factories import LeadFactory

    contact = ContactFactory()
    LeadFactory(contact=contact)  # the history card only renders when there is history
    body = _profile(client, contact)
    assert body.index("Billing accounts") < body.index("Orders &amp; quotes")


def test_the_card_uses_the_shared_partial():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    profile = (root / "templates" / "contacts" / "contact_detail.html").read_text()
    assert 'include "billing/_billing_card.html"' in profile


# --- an account and its groups -------------------------------------------------------


def test_an_account_shows_its_name_and_terms(client, agent):
    account = _account()
    body = _profile(client, account.contact)
    assert "Ridgeline Partners" in body
    assert "Net 30" in body


def test_every_group_gets_a_row_default_first(client, agent):
    account = _account()
    services.add_group(account, name="Marketing — Events")
    services.add_group(account, name="Executive Travel")
    body = _profile(client, account.contact)
    for name in ("Accounts Payable", "Marketing — Events", "Executive Travel"):
        assert name in body
    # default first, then alphabetical — the order AccountGroup.Meta pins
    assert body.index("Accounts Payable") < body.index("Executive Travel") < body.index("Marketing")


def test_a_group_shows_where_its_invoices_go(client, agent):
    account = _account()
    assert "ap@ridgeline.example" in _profile(client, account.contact)


def _row(group, account) -> str:
    """One group row on its own.

    Counting a phrase across the whole page cannot answer this: the modals' terms picker
    lists every option, so "Net 30" is on the page whatever the rows say.
    """
    from django.template.loader import render_to_string

    return render_to_string("billing/_group_row.html", {"group": group, "account": account})


def test_a_group_names_its_terms_only_when_they_differ(client, agent):
    account = _account(terms=Terms.NET_30)
    # the only group inherits, so its row must not repeat the account's terms
    assert "Net 30" not in _row(account.groups.get(), account)

    other = services.add_group(account, name="Marketing", terms=Terms.NET_15)
    assert "Net 15" in _row(other, account)


def test_a_group_set_to_the_same_terms_does_not_repeat_them(client, agent):
    """Having its own terms is not the same as differing from the account's."""
    account = _account(terms=Terms.NET_30)
    group = services.add_group(account, name="Marketing", terms=Terms.NET_30)
    assert "Net 30" not in _row(group, account)


def test_the_po_flag_shows_only_when_it_is_set(client, agent):
    account = _account()
    assert "PO number required" not in _profile(client, account.contact)

    services.add_group(account, name="Marketing", po_required=True)
    assert "PO number required" in _profile(client, account.contact)


def test_the_default_group_is_marked(client, agent):
    account = _account()
    assert "Default" in _profile(client, account.contact)


# --- the numbers, real-shaped and zero until Phase 4 ---------------------------------


def test_the_account_leads_with_its_money(client, agent):
    account = _account()
    body = _profile(client, account.contact)
    for label in ("Open balance", "Not yet invoiced", "Oldest unpaid"):
        assert label in body
    assert "$0.00" in body  # zero, not blank — invoices land in Phase 4


def test_the_figures_come_from_the_selector():
    """Shape now so Phase 4 fills in a number rather than rebuilding the card."""
    from apps.billing import selectors

    account = _account()
    figures = selectors.account_figures(account)
    assert set(figures) == {"open_balance", "uninvoiced_total", "uninvoiced_orders", "oldest_days"}
    assert figures["open_balance"] == 0
    assert figures["oldest_days"] is None


# --- sync chips stay away until QuickBooks exists ------------------------------------


def test_no_sync_chip_while_quickbooks_is_not_connected(client, agent):
    """Every record is NOT_SYNCED in Phase 1; chips would be a wall of red saying nothing."""
    account = _account()
    body = _profile(client, account.contact)
    assert "Not synced" not in body
    assert "In QuickBooks" not in body


def test_the_chips_appear_once_quickbooks_is_connected(client, agent, settings):
    account = _account()
    account.qbo_sync_state = SyncState.SYNCED
    account.save(update_fields=["qbo_sync_state"])
    group = account.groups.get()
    group.qbo_sync_state = SyncState.ERROR
    group.qbo_sync_error = "QuickBooks already has a customer with this name."
    group.save(update_fields=["qbo_sync_state", "qbo_sync_error"])

    settings.QBO_CLIENT_ID = "connected-for-this-test"
    body = _profile(client, account.contact)
    assert "In QuickBooks" in body
    assert "QuickBooks already has a customer with this name." in body
    assert "Retry sync" in body


# --- empty states --------------------------------------------------------------------


def test_a_contact_with_no_account_is_offered_one(client, agent):
    body = _profile(client, ContactFactory())
    assert "No billing account" in body
    assert "New billing account" in body


def test_a_contact_with_no_cards_says_so(client, agent):
    assert "No cards on file" in _profile(client, ContactFactory())


# --- the house rules -----------------------------------------------------------------


def test_the_card_uses_no_native_select_or_confirm(client, agent):
    body = _profile(client, _account().contact)
    assert "window.confirm" not in body
    assert "<dialog" not in body


def test_the_card_costs_no_query_per_group(client, agent):
    """One group or ten, the profile must cost the same."""
    small = _account(contact=ContactFactory())
    _profile(client, small.contact)  # warm anything cached per-process

    with CaptureQueriesContext(connection) as one_group:
        _profile(client, small.contact)

    big = _account(contact=ContactFactory(), name="Beltway")
    for i in range(10):
        services.add_group(big, name=f"Team {i}")
    with CaptureQueriesContext(connection) as ten_groups:
        _profile(client, big.contact)

    assert len(ten_groups) <= len(one_group)
