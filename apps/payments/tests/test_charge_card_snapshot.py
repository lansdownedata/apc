"""Which card actually paid this charge (APC-40).

`PaymentPlan.card_brand` / `card_last4` hold the card *currently* on file, and
`save_payment_method` overwrites them whenever it is replaced. So after a customer swaps
their card, the plan can no longer answer "what paid the deposit?" — only Stripe can.

The answer is a snapshot on the `Charge` itself, taken at the moment the money moved (or
was held). It is wanted for receipts now, for invoices in Phase 4, and it is what makes
removing a card safe in Phase 7.

Brand and last four only. Never a PAN, an expiry or a CVC.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    status = kwargs.pop("lead_status", Lead.Status.QUOTED)
    lead = kwargs.pop("lead", None) or LeadFactory(status=status)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _intent(pi_id="pi_1", *, amount=50000, status="succeeded", brand="visa", last4="4242"):
    card = MagicMock(brand=brand, last4=last4) if brand or last4 else None
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=card),
    )


def _bare_intent(pi_id="pi_1", *, amount=50000, status="succeeded"):
    """What a webhook re-delivery can look like: no payment method resolved at all."""
    return MagicMock(id=pi_id, status=status, amount=amount, payment_method=None)


def _reconcile(plan, pi_id="pi_1", *, kind=Charge.Kind.BALANCE, intent=None):
    with (
        patch.object(
            services.stripe.PaymentIntent, "retrieve", return_value=intent or _intent(pi_id)
        ) as retrieve,
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        charge = services.record_payment(plan, pi_id, kind=kind)
    return charge, retrieve


def _authorize(plan, pi_id="pi_auth", *, intent=None):
    with patch.object(
        services.stripe.PaymentIntent,
        "retrieve",
        return_value=intent or _intent(pi_id, status="requires_capture"),
    ) as retrieve:
        charge = services.record_authorization(plan, pi_id)
    return charge, retrieve


# --- the snapshot is taken ------------------------------------------------------------


def test_a_captured_deposit_records_the_card_that_paid_it():
    plan = _plan()
    charge, _ = _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_a_balance_charge_records_it_too():
    plan = _plan()
    charge, _ = _reconcile(plan, kind=Charge.Kind.BALANCE)
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_a_held_deposit_records_it_at_authorization():
    """No money has moved, but the card is known — and a hold is what the office sees."""
    plan = _plan()
    charge, _ = _authorize(plan)
    charge.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_capturing_a_held_deposit_does_not_blank_it():
    plan = _plan()
    charge, _ = _authorize(plan, "pi_hold")
    _reconcile(plan, "pi_hold", kind=Charge.Kind.DEPOSIT)
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_the_automatic_balance_charge_records_it():
    """`charge_balance` is the `charge-due-balances` cron — the most common balance charge
    there is. It confirms off-session and posts its own ledger entry, so it never reaches
    `record_payment`, and it charges `plan.stripe_payment_method_id` by definition: the
    card on the plan at that moment *is* the card that paid, with no Stripe call needed.
    """
    plan = _plan(
        stripe_payment_method_id="pm_1",
        card_brand="mastercard",
        card_last4="5454",
        balance_status="scheduled",
    )
    intent = MagicMock(id="pi_bal", status="succeeded", amount=50000)
    with patch.object(services.stripe.PaymentIntent, "create", return_value=intent):
        charge = services.charge_balance(plan)
    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED
    assert (charge.card_brand, charge.card_last4) == ("mastercard", "5454")


def test_the_automatic_balance_charge_records_nothing_when_no_card_is_known():
    plan = _plan(stripe_payment_method_id="pm_1", card_brand="", card_last4="")
    intent = MagicMock(id="pi_bal", status="succeeded", amount=50000)
    with patch.object(services.stripe.PaymentIntent, "create", return_value=intent):
        charge = services.charge_balance(plan)
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("", "")


# --- and it does not drift ------------------------------------------------------------


def test_replacing_the_card_leaves_earlier_charges_on_the_old_one():
    """The whole reason the snapshot exists."""
    plan = _plan()
    deposit, _ = _reconcile(plan, "pi_dep", kind=Charge.Kind.DEPOSIT)

    new_card = MagicMock(id="pm_2", customer="cus_1", card=MagicMock(brand="amex", last4="0005"))
    with patch.object(services.stripe.PaymentMethod, "retrieve", return_value=new_card):
        services.save_payment_method(plan, "pm_2")

    plan.refresh_from_db()
    deposit.refresh_from_db()
    assert (plan.card_brand, plan.card_last4) == ("amex", "0005")  # the card on file moved
    assert (deposit.card_brand, deposit.card_last4) == ("visa", "4242")  # the charge did not


def test_a_redelivered_webhook_without_card_detail_does_not_blank_it():
    """Stripe re-delivers; nothing about the charge changed. Writing "" would lose the card."""
    plan = _plan()
    charge, _ = _reconcile(plan, "pi_1", kind=Charge.Kind.DEPOSIT)
    _reconcile(plan, "pi_1", kind=Charge.Kind.DEPOSIT, intent=_bare_intent("pi_1"))
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_a_payment_method_with_no_card_does_not_blank_it():
    """A non-card method (or one Stripe did not expand) is not evidence the card changed."""
    plan = _plan()
    charge, _ = _reconcile(plan, "pi_1", kind=Charge.Kind.DEPOSIT)
    _reconcile(plan, "pi_1", kind=Charge.Kind.DEPOSIT, intent=_intent("pi_1", brand="", last4=""))
    charge.refresh_from_db()
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")


def test_a_charge_that_never_succeeded_records_nothing():
    plan = _plan()
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("100.00"))
    services._record_failure(plan, charge, services.PaymentError("card_declined"))
    charge.refresh_from_db()
    assert charge.status == Charge.Status.FAILED
    assert (charge.card_brand, charge.card_last4) == ("", "")


def test_historical_charges_are_simply_blank():
    """No backfill, and nothing calls Stripe to invent one."""
    plan = _plan()
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    assert (charge.card_brand, charge.card_last4) == ("", "")


# --- and it costs nothing -------------------------------------------------------------


def test_the_snapshot_adds_no_stripe_call():
    """Both paths already retrieve the intent with `expand=["payment_method"]`. This has to
    reuse that object, not ask Stripe again."""
    plan = _plan()
    _, captured = _reconcile(plan, "pi_1", kind=Charge.Kind.DEPOSIT)
    assert captured.call_count == 1

    _, held = _authorize(_plan(), "pi_2")
    assert held.call_count == 1


# --- what the office sees -------------------------------------------------------------


def _money_card(charges) -> str:
    from django.template.loader import render_to_string

    return render_to_string("payments/_money_card.html", {"charges": charges})


def test_the_money_card_names_the_card_beside_a_charge_that_has_one():
    plan = _plan()
    charge, _ = _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    charge.refresh_from_db()
    body = _money_card([charge])
    assert "Visa" in body
    assert "4242" in body


def test_the_money_card_says_nothing_beside_a_charge_that_does_not():
    plan = _plan()
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    assert "••••" not in _money_card([charge])
