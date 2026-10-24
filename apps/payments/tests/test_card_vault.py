"""APC-47 — the customer-level card vault.

Cards belong to the customer: one Stripe customer per contact, saved cards with brand,
last four and expiry only, and a Remove that can never make a balance charge fail.
Stripe is mocked throughout; nothing here calls it for real.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from apps.contacts.factories import ContactFactory
from apps.payments.models import SavedCard

pytestmark = pytest.mark.django_db


def _card(contact=None, **kw):
    kw.setdefault("stripe_payment_method_id", f"pm_{SavedCard.objects.count() + 1}")
    kw.setdefault("brand", "visa")
    kw.setdefault("last4", "4242")
    kw.setdefault("exp_month", 8)
    kw.setdefault("exp_year", 2028)
    return SavedCard.objects.create(contact=contact or ContactFactory(), **kw)


# --- Task 1: the model ----------------------------------------------------------------


def test_a_contact_has_no_stripe_customer_until_one_is_needed():
    assert ContactFactory().stripe_customer_id == ""


def test_the_expiry_label_is_month_slash_two_digit_year():
    assert _card(exp_month=8, exp_year=2028).exp_label == "08/28"


@pytest.mark.parametrize(
    ("today", "exp", "soon", "expired"),
    [
        (date(2026, 10, 24), (11, 2026), True, False),  # ends next month: flag it
        (date(2026, 10, 24), (10, 2026), True, False),  # ends this month: still valid
        (date(2026, 10, 24), (1, 2027), False, False),  # three months out: fine
        (date(2026, 10, 24), (9, 2026), False, True),  # last month: expired, not "soon"
    ],
)
def test_expiry_flags(today, exp, soon, expired):
    card = _card(exp_month=exp[0], exp_year=exp[1])

    with patch("apps.payments.models.timezone.localdate", return_value=today):
        assert (card.expires_soon, card.expired) == (soon, expired)


def test_only_brand_last_four_and_expiry_are_stored():
    fields = {f.name for f in SavedCard._meta.get_fields() if getattr(f, "concrete", False)}

    assert not {"number", "pan", "cvc", "cvv", "fingerprint"} & fields
    assert {"brand", "last4", "exp_month", "exp_year"} <= fields


# --- Task 2: the services ---------------------------------------------------------------

from apps.leads.factories import LeadFactory  # noqa: E402
from apps.leads.models import Lead  # noqa: E402
from apps.payments import cards, services, tasks  # noqa: E402
from apps.payments.factories import PaymentPlanFactory  # noqa: E402
from apps.payments.models import Charge, PaymentPlan  # noqa: E402

STRIPE = services.stripe


def _pm(pm_id="pm_new", customer="cus_contact", brand="visa", last4="4242", month=8, year=2028):
    return MagicMock(
        id=pm_id,
        customer=customer,
        card=MagicMock(brand=brand, last4=last4, exp_month=month, exp_year=year),
    )


def _setup_intent(pm, *, customer="cus_contact", status="succeeded"):
    return MagicMock(id="seti_1", status=status, customer=customer, payment_method=pm)


def _contact_with_customer(customer="cus_contact"):
    return ContactFactory(stripe_customer_id=customer)


def _booked_plan(
    contact, *, pm="", customer="cus_contact", balance=PaymentPlan.BalanceStatus.SCHEDULED
):
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=contact)
    return PaymentPlanFactory(
        lead=lead,
        stripe_customer_id=customer,
        stripe_payment_method_id=pm,
        card_brand="visa" if pm else "",
        card_last4="4242" if pm else "",
        balance_status=balance,
    )


def test_the_contacts_stripe_customer_is_created_once():
    contact = ContactFactory()

    with patch.object(STRIPE.Customer, "create", return_value=MagicMock(id="cus_1")) as create:
        assert cards.contact_customer(contact) == "cus_1"
        assert cards.contact_customer(contact) == "cus_1"

    create.assert_called_once()
    contact.refresh_from_db()
    assert contact.stripe_customer_id == "cus_1"


def test_adding_a_card_starts_an_off_session_setup_intent_on_the_contacts_customer():
    contact = _contact_with_customer()

    with patch.object(
        STRIPE.SetupIntent, "create", return_value=MagicMock(client_secret="s_1")
    ) as create:
        assert cards.start_add_card(contact) == "s_1"

    kwargs = create.call_args.kwargs
    assert (kwargs["customer"], kwargs["usage"], kwargs["payment_method_types"]) == (
        "cus_contact",
        "off_session",
        ["card"],
    )


def test_finishing_records_brand_last_four_and_expiry_and_the_first_card_is_default():
    contact = _contact_with_customer()

    with patch.object(STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm())):
        card = cards.finish_add_card(contact, "seti_1")

    assert (card.brand, card.last4, card.exp_label, card.is_default) == (
        "visa",
        "4242",
        "08/28",
        True,
    )
    assert card.contact == contact


def test_finishing_is_idempotent():
    contact = _contact_with_customer()

    with patch.object(STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm())):
        first = cards.finish_add_card(contact, "seti_1")
        second = cards.finish_add_card(contact, "seti_1")

    assert first.pk == second.pk
    assert SavedCard.objects.filter(contact=contact).count() == 1


def test_a_setup_intent_for_another_customer_is_refused():
    contact = _contact_with_customer()

    with patch.object(
        STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm(), customer="cus_other")
    ):
        with pytest.raises(cards.CardError):
            cards.finish_add_card(contact, "seti_1")

    assert not SavedCard.objects.exists()


def test_an_unfinished_setup_intent_is_refused():
    contact = _contact_with_customer()

    with patch.object(
        STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm(), status="requires_action")
    ):
        with pytest.raises(cards.CardError):
            cards.finish_add_card(contact, "seti_1")


def test_make_default_leaves_exactly_one_default():
    contact = _contact_with_customer()
    a = _card(contact, is_default=True)
    b = _card(contact)
    SavedCard.objects.filter(pk=b.pk).update(is_default=True)  # a race left two

    cards.make_default(a)

    assert list(SavedCard.objects.filter(contact=contact, is_default=True)) == [a]


def test_removing_a_card_clears_it_from_orders_with_a_balance_due_and_names_them():
    contact = _contact_with_customer()
    card = _card(contact, stripe_payment_method_id="pm_x", is_default=True)
    open_plan = _booked_plan(contact, pm="pm_x")
    paid_plan = _booked_plan(contact, pm="pm_x", balance=PaymentPlan.BalanceStatus.PAID)

    assert [p.pk for p in cards.plans_holding(card)] == [open_plan.pk]
    with patch.object(STRIPE.PaymentMethod, "detach") as detach:
        cleared = cards.remove_card(card)

    assert [lead.pk for lead in cleared] == [open_plan.lead_id]
    detach.assert_called_once_with("pm_x")
    open_plan.refresh_from_db()
    paid_plan.refresh_from_db()
    assert (open_plan.stripe_payment_method_id, open_plan.card_last4) == ("", "")
    assert paid_plan.stripe_payment_method_id == "pm_x"  # nothing more to charge it for
    assert not SavedCard.objects.filter(pk=card.pk).exists()


def test_after_removal_the_balance_cron_skips_instead_of_failing():
    contact = _contact_with_customer()
    card = _card(contact, stripe_payment_method_id="pm_x")
    _booked_plan(contact, pm="pm_x")

    with patch.object(STRIPE.PaymentMethod, "detach"):
        cards.remove_card(card)
    with (
        patch.object(PaymentPlan, "balance_due_now", True),
        patch.object(services, "charge_balance") as charge,
    ):
        tasks.charge_due_balances()

    charge.assert_not_called()


def test_removal_keeps_what_past_charges_were_paid_with():
    contact = _contact_with_customer()
    card = _card(contact, stripe_payment_method_id="pm_x")
    plan = _booked_plan(contact, pm="pm_x")
    paid = plan.charges.create(
        kind=Charge.Kind.DEPOSIT,
        amount="100.00",
        status=Charge.Status.SUCCEEDED,
        card_brand="visa",
        card_last4="4242",
        idempotency_key="seed-dep",
    )

    with patch.object(STRIPE.PaymentMethod, "detach"):
        cards.remove_card(card)

    paid.refresh_from_db()
    assert (paid.card_brand, paid.card_last4) == ("visa", "4242")


def test_removing_the_default_promotes_the_most_recently_used():
    from datetime import timedelta

    from django.utils import timezone

    contact = _contact_with_customer()
    default = _card(contact, is_default=True)
    old = _card(contact, last_used_at=timezone.now() - timedelta(days=30))
    recent = _card(contact, last_used_at=timezone.now() - timedelta(days=1))

    with patch.object(STRIPE.PaymentMethod, "detach"):
        cards.remove_card(default)

    recent.refresh_from_db()
    old.refresh_from_db()
    assert (recent.is_default, old.is_default) == (True, False)


def test_a_new_order_uses_the_contacts_customer_and_default_card():
    contact = _contact_with_customer()
    _card(contact, stripe_payment_method_id="pm_def", is_default=True, last4="1111")
    plan = PaymentPlanFactory(lead=LeadFactory(contact=contact))

    with patch.object(STRIPE.Customer, "create") as create:
        assert services.get_or_create_customer(plan) == "cus_contact"

    create.assert_not_called()
    plan.refresh_from_db()
    assert (plan.stripe_customer_id, plan.stripe_payment_method_id, plan.card_last4) == (
        "cus_contact",
        "pm_def",
        "1111",
    )


def test_an_order_on_its_own_legacy_customer_never_adopts_the_default_card():
    contact = _contact_with_customer()
    _card(contact, stripe_payment_method_id="pm_def", is_default=True)
    plan = PaymentPlanFactory(lead=LeadFactory(contact=contact), stripe_customer_id="cus_legacy")

    assert services.get_or_create_customer(plan) == "cus_legacy"

    plan.refresh_from_db()
    assert plan.stripe_payment_method_id == ""


def test_paying_with_a_card_on_the_contacts_customer_saves_it_to_the_vault():
    contact = _contact_with_customer()
    plan = _booked_plan(contact)

    services._store_card(plan, _pm(pm_id="pm_paid", last4="9999"))

    card = SavedCard.objects.get(stripe_payment_method_id="pm_paid")
    assert (card.contact, card.last4, card.last_used_at is not None) == (contact, "9999", True)


def test_paying_on_a_legacy_customer_does_not_touch_the_vault():
    contact = _contact_with_customer()
    plan = _booked_plan(contact, customer="cus_legacy")

    services._store_card(plan, _pm(pm_id="pm_old", customer="cus_legacy"))

    assert not SavedCard.objects.exists()


# --- Task 3 + 4: endpoints and the Cards tab --------------------------------------------

from django.urls import reverse  # noqa: E402

from apps.accounts.factories import UserFactory  # noqa: E402


@pytest.fixture
def cashier(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def test_the_setup_endpoint_returns_a_client_secret(cashier):
    contact = _contact_with_customer()

    with patch.object(STRIPE.SetupIntent, "create", return_value=MagicMock(client_secret="s_1")):
        resp = cashier.post(reverse("billing_card_setup", args=[contact.pk]))

    assert resp.json() == {"ok": True, "client_secret": "s_1"}


def test_card_actions_need_payments_access(client):
    contact = _contact_with_customer()
    card = _card(contact)
    client.force_login(UserFactory())

    for url in (
        reverse("billing_card_setup", args=[contact.pk]),
        reverse("billing_card_complete", args=[contact.pk]),
        reverse("billing_card_default", args=[card.pk]),
        reverse("billing_card_remove", args=[card.pk]),
    ):
        assert client.post(url).status_code in (302, 403)


def test_the_complete_endpoint_records_the_card(cashier):
    contact = _contact_with_customer()

    with patch.object(STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm())):
        resp = cashier.post(
            reverse("billing_card_complete", args=[contact.pk]), {"setup_intent": "seti_1"}
        )

    assert resp.json()["ok"] is True
    assert SavedCard.objects.filter(contact=contact, last4="4242").exists()


def test_a_refused_card_is_a_400_with_the_reason(cashier):
    contact = _contact_with_customer()

    with patch.object(
        STRIPE.SetupIntent, "retrieve", return_value=_setup_intent(_pm(), customer="cus_other")
    ):
        resp = cashier.post(
            reverse("billing_card_complete", args=[contact.pk]), {"setup_intent": "seti_1"}
        )

    assert resp.status_code == 400
    assert "different customer" in resp.json()["error"]


def test_a_stripe_failure_is_a_502(cashier):
    import stripe

    contact = _contact_with_customer()

    with patch.object(
        STRIPE.SetupIntent, "create", side_effect=stripe.error.APIConnectionError("down")
    ):
        resp = cashier.post(reverse("billing_card_setup", args=[contact.pk]))

    assert resp.status_code == 502


def test_the_default_and_remove_endpoints(cashier):
    contact = _contact_with_customer()
    a = _card(contact, is_default=True, stripe_payment_method_id="pm_a")
    b = _card(contact, stripe_payment_method_id="pm_b")
    plan = _booked_plan(contact, pm="pm_a")

    assert cashier.post(reverse("billing_card_default", args=[b.pk])).json()["ok"] is True
    with patch.object(STRIPE.PaymentMethod, "detach"):
        resp = cashier.post(reverse("billing_card_remove", args=[a.pk]))

    assert resp.json() == {"ok": True, "cleared": [plan.lead.quote_no]}
    b.refresh_from_db()
    assert b.is_default is True


def _tab(client, contact):
    return client.get(reverse("contact_detail", args=[contact.pk])).content.decode()


def test_the_cards_tab_shows_the_vault(cashier):
    contact = _contact_with_customer()
    _card(contact, is_default=True, exp_month=8, exp_year=2028, last4="4242")
    with patch("apps.payments.models.timezone.localdate", return_value=date(2026, 10, 24)):
        _card(contact, exp_month=11, exp_year=2026, last4="1881")
        html = _tab(cashier, contact)

    assert "Default" in html
    assert "Expires 08/28" in html
    assert "Expires 11/26" in html and "chip-warn" in html
    assert "Add card" in html and "data-card-number" in html


def test_the_remove_confirm_names_the_orders_it_will_clear(cashier):
    contact = _contact_with_customer()
    _card(contact, stripe_payment_method_id="pm_x", is_default=True)
    plan = _booked_plan(contact, pm="pm_x")

    html = _tab(cashier, contact)

    assert f'data-open-orders="{plan.lead.quote_no}"' in html  # carried into the confirm


def test_cards_from_old_orders_on_their_own_customer_are_listed_read_only(cashier):
    contact = _contact_with_customer()
    _booked_plan(contact, pm="pm_legacy", customer="cus_legacy")  # visa 4242, not in the vault

    html = _tab(cashier, contact)

    assert "From earlier orders" in html


def test_staff_without_payments_access_see_cards_but_no_actions(client):
    contact = _contact_with_customer()
    _card(contact, is_default=True)
    client.force_login(UserFactory())

    html = _tab(client, contact)

    assert "Expires 08/28" in html
    assert "Add card" not in html and "Make default" not in html
