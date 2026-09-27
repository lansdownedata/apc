"""The Cards tab on a customer's profile (APC-39).

Display-only, by decision. There is no card store on the contact — `PaymentPlan` is one
per order and holds the card that order used, so "this customer's cards" is something we
derive, not something we own. Adding, removing and choosing a default all need the
customer-level vault (APC-47), and removing is unsafe until then: the `charge-due-balances`
cron charges `plan.stripe_payment_method_id` 30 days before pickup, so detaching a card in
Stripe while a plan still points at it makes that charge fail.

Two sources, because neither alone is complete:

* `Charge` (since APC-40) is what actually paid, immutable and dated — the honest answer to
  "last used", and it keeps cards a later swap would otherwise erase.
* `PaymentPlan` is the card currently on file. It covers charges that predate APC-40, and a
  card saved on an order that has not been charged yet.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import selectors
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory(can_manage_payments=True))


def _plan(contact, *, brand="visa", last4="4242", status=Lead.Status.BOOKED, **over):
    lead = LeadFactory(contact=contact, status=status)
    return PaymentPlanFactory(
        lead=lead,
        quote_total=Decimal("1000.00"),
        card_brand=brand,
        card_last4=last4,
        **over,
    )


def _charge(plan, *, brand="visa", last4="4242", when=None, status=Charge.Status.SUCCEEDED):
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.card_brand = brand
    charge.card_last4 = last4
    charge.status = status
    charge.save(update_fields=["card_brand", "card_last4", "status", "updated_at"])
    if when is not None:
        # auto_now_add, so it cannot be set on create
        Charge.objects.filter(pk=charge.pk).update(created_at=when)
        charge.refresh_from_db()
    return charge


def _profile(client, contact) -> str:
    return client.get(reverse("contact_detail", args=[contact.pk])).content.decode()


# --- one card, however many orders used it -------------------------------------------


def test_two_orders_on_the_same_card_are_one_row():
    contact = ContactFactory()
    now = timezone.now()
    old = _plan(contact)
    _charge(old, when=now - timedelta(days=90))
    recent = _plan(contact)
    _charge(recent, when=now - timedelta(days=2))

    cards = selectors.cards_for(contact)
    assert len(cards) == 1
    assert cards[0]["last4"] == "4242"
    assert cards[0]["lead"] == recent.lead  # named by the more recent use


def test_different_cards_get_their_own_rows_most_recent_first():
    contact = ContactFactory()
    now = timezone.now()
    _charge(
        _plan(contact, brand="amex", last4="1005"),
        brand="amex",
        last4="1005",
        when=now - timedelta(days=60),
    )
    _charge(_plan(contact), when=now - timedelta(days=1))

    cards = selectors.cards_for(contact)
    assert [c["last4"] for c in cards] == ["4242", "1005"]


def test_another_customers_card_never_appears():
    mine = ContactFactory()
    _charge(_plan(ContactFactory(), brand="amex", last4="1005"), brand="amex", last4="1005")
    _charge(_plan(mine))
    assert [c["last4"] for c in selectors.cards_for(mine)] == ["4242"]


# --- the two sources ------------------------------------------------------------------


def test_a_card_on_file_with_no_charge_still_shows():
    """A deposit link sent and a card saved, nothing charged yet."""
    contact = ContactFactory()
    _plan(contact, last4="9999")
    assert [c["last4"] for c in selectors.cards_for(contact)] == ["9999"]


def test_a_card_that_has_never_been_charged_does_not_claim_it_was_used(client, agent):
    """It was saved, not used. On a money screen the difference is worth the extra word."""
    contact = ContactFactory()
    plan = _plan(contact, last4="9999")
    assert selectors.cards_for(contact)[0]["used"] is False

    body = _profile(client, contact)
    assert "on file for" in body
    assert "last used on" not in body
    assert plan.lead.quote_no in body


def test_a_card_that_has_been_charged_says_so(client, agent):
    contact = ContactFactory()
    _charge(_plan(contact))
    assert selectors.cards_for(contact)[0]["used"] is True
    assert "last used on" in _profile(client, contact)


def test_a_card_the_plan_has_since_replaced_is_not_lost():
    """The whole reason APC-40 snapshots the charge. The plan only knows the new card."""
    contact = ContactFactory()
    plan = _plan(contact, brand="amex", last4="1005")  # what the plan holds NOW
    _charge(plan, brand="visa", last4="4242")  # what actually paid, before the swap

    assert sorted(c["last4"] for c in selectors.cards_for(contact)) == ["1005", "4242"]


def test_a_plan_with_no_card_contributes_nothing():
    contact = ContactFactory()
    _plan(contact, brand="", last4="")
    assert selectors.cards_for(contact) == []


def test_a_charge_that_never_went_through_contributes_nothing():
    contact = ContactFactory()
    plan = _plan(contact, brand="", last4="")
    _charge(plan, status=Charge.Status.FAILED)
    assert selectors.cards_for(contact) == []


def test_a_held_deposit_counts_as_used():
    """The money is on hold, not captured — but that card was certainly used."""
    contact = ContactFactory()
    plan = _plan(contact, brand="", last4="", status=Lead.Status.ENGAGED)
    _charge(plan, status=Charge.Status.AUTHORIZED)
    assert [c["last4"] for c in selectors.cards_for(contact)] == ["4242"]


# --- where a row points ---------------------------------------------------------------


def test_a_booked_order_links_to_the_order_page(client, agent):
    contact = ContactFactory()
    plan = _plan(contact, status=Lead.Status.BOOKED)
    _charge(plan)
    assert reverse("order_detail", args=[plan.lead.pk]) in _profile(client, contact)


def test_a_quote_links_to_the_quote(client, agent):
    contact = ContactFactory()
    plan = _plan(contact, status=Lead.Status.QUOTED)
    _charge(plan)
    body = _profile(client, contact)
    assert reverse("lead_detail", args=[plan.lead.pk]) in body


# --- what the tab renders -------------------------------------------------------------


def test_the_row_shows_the_brand_and_the_last_four(client, agent):
    contact = ContactFactory()
    _charge(_plan(contact))
    body = _profile(client, contact)
    assert "VISA" in body
    assert "4242" in body


def test_the_row_names_where_it_was_last_used(client, agent):
    contact = ContactFactory()
    plan = _plan(contact)
    _charge(plan)
    assert plan.lead.quote_no in _profile(client, contact)


def test_the_footer_says_what_we_keep(client, agent):
    contact = ContactFactory()
    _charge(_plan(contact))
    body = _profile(client, contact)
    assert "Card numbers are held by Stripe. We keep the brand and the last four" in body


def test_a_contact_with_no_cards_still_gets_the_empty_state(client, agent):
    body = _profile(client, ContactFactory())
    assert "No cards on file" in body
    assert "A card is saved the first time this customer pays a deposit." in body


def test_the_tab_counts_the_cards(client, agent):
    contact = ContactFactory()
    _charge(_plan(contact))
    _charge(_plan(contact, brand="amex", last4="1005"), brand="amex", last4="1005")
    assert len(selectors.billing_context(contact)["billing_cards"]) == 2


# --- what it must NOT render ----------------------------------------------------------


def test_nothing_offers_to_add_remove_or_default_a_card():
    """All three need the customer-level vault (APC-47), and Remove is unsafe before it:
    the balance cron charges the plan's saved card 30 days out.

    Rendered on its own rather than asserted against the whole profile: "Make default" is a
    legitimate control on the *accounts* tab, so a page-wide search would only pass by
    accident on a contact that happens to have no billing account.
    """
    from django.template.loader import render_to_string

    contact = ContactFactory()
    _charge(_plan(contact))
    tab = render_to_string("billing/_cards_tab.html", selectors.billing_context(contact))
    for control in ("Add card", "Remove", "Make default", "<button"):
        assert control not in tab, control


def test_no_expiry_is_shown(client, agent):
    """We do not store one, and inventing it — or calling Stripe per row — is worse."""
    contact = ContactFactory()
    _charge(_plan(contact))
    assert "Expires" not in _profile(client, contact)


def test_nothing_but_the_last_four_of_the_number_reaches_the_page(client, agent):
    contact = ContactFactory()
    plan = _plan(contact)
    _charge(plan)
    cards = selectors.cards_for(contact)
    assert set(cards[0]) == {"brand", "last4", "lead", "used_at", "used"}
    assert len(cards[0]["last4"]) <= 4


# --- cost ------------------------------------------------------------------------------


def test_the_tab_costs_the_same_for_one_order_or_ten(client, agent):
    small = ContactFactory()
    _charge(_plan(small))
    _profile(client, small)  # warm anything cached per-process

    with CaptureQueriesContext(connection) as one:
        _profile(client, small)

    big = ContactFactory()
    for i in range(10):
        _charge(_plan(big, last4=f"{4000 + i}"), last4=f"{4000 + i}")
    with CaptureQueriesContext(connection) as ten:
        _profile(client, big)

    assert len(ten) <= len(one)
