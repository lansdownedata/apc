"""APC-60 — the final bill: one charge per order for the approved customer overtime plus
whatever is left of the order balance, once every trip is reviewed.

Ledger (Moe, 2026-09-27): the balance part posts exactly as a balance payment does
(`post_capture`); the overtime posts at capture straight to Recognized Revenue — the trip
has run, so there's nothing to defer — as two lines, base and gratuity.

Stripe is mocked throughout, and the key is blanked so an unmocked call can't reach it.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.core.choices import Account
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import ledger, services, tasks, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan
from apps.reservations import review_queue, reviews
from apps.reservations.factories import ReservationFactory
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_real_stripe(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def accountant(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def _order(*, trips=1, paid=True, card=True):
    """A booked order whose trips ran yesterday (3h at $200/h, 20% gratuity → $720 each),
    in review. `paid` = the whole order total already collected; else only the deposit."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    made = []
    for _ in range(trips):
        trip = ReservationFactory(
            lead=lead,
            pickup_date=timezone.localdate() - timedelta(days=1),
            pickup_time=time(18, 0),
            pickup_timezone="America/New_York",
            rate=Decimal("200"),
            hours=Decimal("3"),
            gratuity_pct=Decimal("20"),
        )
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
        made.append(trip)
    lead.refresh_from_db()
    plan = PaymentPlanFactory(
        lead=lead,
        quote_total=lead.quote_total,
        stripe_customer_id="cus_1" if card else "",
        stripe_payment_method_id="pm_1" if card else "",
        card_brand="visa" if card else "",
        card_last4="4242" if card else "",
        deposit_status=PaymentPlan.DepositStatus.PAID,
        balance_status=(
            PaymentPlan.BalanceStatus.PAID if paid else PaymentPlan.BalanceStatus.SCHEDULED
        ),
    )
    collected = plan.quote_total if paid else plan.deposit_amount
    ledger.post_capture(
        lead=lead,
        amount=collected,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"seed-{lead.pk}",
    )
    run_tasks()
    return lead, plan, made


def _review(trip, minutes=45, *, complete=True):
    start = trip.pickup_at
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=minutes),
        billable_overtime_minutes=minutes,
    )
    if complete:
        reviews.complete_review(review, user=UserFactory())
    return review


def _intent(pi_id="pi_final", *, amount=18000, status="succeeded"):
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        client_secret=f"{pi_id}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _stripe(intent=None):
    intent = intent or _intent()
    return (
        patch.object(stripe.PaymentIntent, "create", return_value=intent),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=intent),
    )


def _charge(lead, intent=None):
    create_p, retrieve_p = _stripe(intent)
    with create_p as create, retrieve_p:
        charge = services.charge_final(lead)
    return charge, create


def _lines(kind):
    entry = JournalEntry.objects.get(kind=kind)
    assert entry.is_balanced
    return sorted((line.account, line.debit, line.credit) for line in entry.lines.all())


# --- what the final bill is ----------------------------------------------------------


def test_the_bill_is_approved_overtime_plus_the_remaining_balance():
    lead, plan, (trip,) = _order(paid=False)
    _review(trip, 45)  # $150 base + $30 gratuity

    bill = services.final_bill(lead)

    assert bill.overtime_base == Decimal("150.00")
    assert bill.overtime_gratuity == Decimal("30.00")
    assert bill.order_balance == plan.balance_amount
    assert bill.amount == plan.balance_amount + Decimal("180.00")
    assert bill.ready


def test_a_paid_order_bills_only_the_overtime():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)

    bill = services.final_bill(lead)

    assert (bill.order_balance, bill.amount) == (Decimal("0.00"), Decimal("180.00"))


def test_totals_and_remaining_include_approved_overtime_everywhere(accountant):
    lead, plan, (trip,) = _order()
    _review(trip, 45)

    assert services.remaining_balance(lead) == Decimal("180.00")
    row = review_queue.order_review(lead.pk)
    assert row.total_due == plan.quote_total + Decimal("180.00")
    assert row.remaining == Decimal("180.00")
    order_page = accountant.get(reverse("order_detail", args=[lead.pk])).content.decode()
    assert "$180.00" in order_page
    listing = accountant.get(reverse("trip_review_list") + "?stage=billing").content.decode()
    assert "$180.00" in listing


def test_an_unfinished_review_is_not_approved_overtime():
    lead, _plan, (trip,) = _order()
    _review(trip, 45, complete=False)

    assert services.remaining_balance(lead) == Decimal("0.00")


# --- refused until ready -------------------------------------------------------------


def test_charging_is_refused_until_every_trip_is_reviewed():
    lead, _plan, (a, _b) = _order(trips=2)
    _review(a, 45)

    assert not services.final_bill(lead).ready
    create_p, retrieve_p = _stripe()
    with create_p as create, retrieve_p, pytest.raises(services.PaymentError, match="review"):
        services.charge_final(lead)
    create.assert_not_called()


def test_the_endpoints_are_refused_for_a_non_payments_user(client):
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    client.force_login(UserFactory())

    for name in ("trip_review_charge", "trip_review_send_link"):
        assert client.post(reverse(name, args=[lead.pk])).status_code == 403


def test_the_charge_endpoint_refuses_an_unreviewed_order(accountant):
    lead, _plan, (a, _b) = _order(trips=2)
    _review(a, 45)

    resp = accountant.post(reverse("trip_review_charge", args=[lead.pk]))

    assert resp.status_code == 400
    assert "review" in resp.json()["error"]


def test_the_send_link_endpoint_refuses_an_unreviewed_order(accountant):
    lead, _plan, (a, _b) = _order(trips=2)
    _review(a, 45)

    with patch("apps.integrations.podium.send_message") as send:
        resp = accountant.post(reverse("trip_review_send_link", args=[lead.pk]))

    assert resp.status_code == 400
    send.assert_not_called()


def test_nothing_to_charge_is_refused():
    lead, _plan, (trip,) = _order()
    _review(trip, 0)

    with pytest.raises(services.PaymentError, match="Nothing"):
        services.charge_final(lead)


def test_a_card_less_order_cannot_be_charged_off_session():
    lead, _plan, (trip,) = _order(card=False)
    _review(trip, 45)

    with pytest.raises(services.PaymentError, match="card"):
        services.charge_final(lead)


# --- the off-session charge ----------------------------------------------------------


def test_charging_takes_the_bill_off_the_card_and_splits_the_ledger():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)

    charge, create = _charge(lead)

    kwargs = create.call_args.kwargs
    assert kwargs["amount"] == 18000
    assert kwargs["off_session"] is True and kwargs["confirm"] is True
    assert kwargs["metadata"]["kind"] == "final"
    charge.refresh_from_db()
    assert (charge.kind, charge.status) == (Charge.Kind.FINAL, Charge.Status.SUCCEEDED)
    assert (charge.overtime_amount, charge.overtime_gratuity) == (
        Decimal("180.00"),
        Decimal("30.00"),
    )
    assert (charge.card_brand, charge.card_last4) == ("visa", "4242")
    assert _lines(JournalEntry.Kind.OVERTIME_CAPTURED) == sorted(
        [
            (Account.CASH, Decimal("180.00"), Decimal("0.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("150.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("30.00")),
        ]
    )
    assert not JournalEntry.objects.filter(
        kind=JournalEntry.Kind.BALANCE_CAPTURED, charge=charge
    ).exists()
    assert services.remaining_balance(lead) == Decimal("0.00")


def test_a_card_on_file_order_with_a_balance_posts_both_parts():
    lead, plan, (trip,) = _order(paid=False)
    _review(trip, 45)
    balance = plan.balance_amount

    charge, _ = _charge(lead, _intent(amount=int((balance + Decimal("180")) * 100)))

    balance_entry = JournalEntry.objects.get(kind=JournalEntry.Kind.BALANCE_CAPTURED, charge=charge)
    assert balance_entry.is_balanced
    cash = sum(line.debit for line in balance_entry.lines.all() if line.account == Account.CASH)
    assert cash == balance
    assert JournalEntry.objects.get(kind=JournalEntry.Kind.OVERTIME_CAPTURED).is_balanced
    assert ledger.order_balances(lead)["collected"] == plan.quote_total + Decimal("180.00")
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID


def test_a_retry_makes_one_stripe_call_and_one_charge():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    create_p, retrieve_p = _stripe()

    with create_p as create, retrieve_p:
        first = services.charge_final(lead)
        second = services.charge_final(lead)

    assert create.call_count == 1
    assert first.pk == second.pk
    assert Charge.objects.filter(kind=Charge.Kind.FINAL).count() == 1


def test_success_closes_overtime_invoiced():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    assert Task.objects.get(reservation=trip, kind="overtime_invoiced").status == "open"

    _charge(lead)

    assert Task.objects.get(reservation=trip, kind="overtime_invoiced").status == "done"


def test_a_decline_leaves_tasks_open_and_raises_the_alert():
    lead, plan, (trip,) = _order()
    _review(trip, 45)
    declined = stripe.error.CardError("Your card was declined.", None, "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=declined):
        charge = services.charge_final(lead)

    charge.refresh_from_db()
    assert charge.status == Charge.Status.FAILED
    assert Task.objects.get(reservation=trip, kind="overtime_invoiced").status == "open"
    lead.refresh_from_db()
    assert lead.has_alert
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.BALANCE_FAILED).exists()
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID  # the booked balance was paid
    assert services.remaining_balance(lead) == Decimal("180.00")


def test_after_a_decline_a_new_attempt_is_made():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    declined = stripe.error.CardError("Declined", None, "card_declined")
    with patch.object(stripe.PaymentIntent, "create", side_effect=declined):
        services.charge_final(lead)

    charge, create = _charge(lead)

    assert charge.status == Charge.Status.SUCCEEDED
    assert create.call_args.kwargs["idempotency_key"].endswith("-2")


# --- the payment link (webhook) path -------------------------------------------------


def test_the_pay_page_asks_for_the_final_bill_once_every_trip_is_reviewed(client):
    from apps.leads import services as lead_services

    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    token = lead_services.make_deposit_token(lead)

    body = client.get(reverse("quote_pay", args=[token])).content.decode()

    assert "Final bill" in body and "$180.00" in body


def test_the_pay_page_holds_overtime_back_until_every_trip_is_reviewed(client):
    from apps.leads import services as lead_services

    lead, plan, (a, _b) = _order(trips=2, paid=False)
    _review(a, 45)
    token = lead_services.make_deposit_token(lead)

    body = client.get(reverse("quote_pay", args=[token])).content.decode()

    assert "Final bill" not in body
    assert f"${plan.balance_amount:,.2f}" in body


def test_paying_the_link_splits_the_ledger_the_same_way(client):
    from apps.leads import services as lead_services

    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    token = lead_services.make_deposit_token(lead)
    create_p, retrieve_p = _stripe()
    with create_p as create, retrieve_p:
        resp = client.post(reverse("quote_pay_intent", args=[token]))
        assert resp.status_code == 200, resp.content
        assert create.call_args.kwargs["metadata"]["kind"] == "final"
        assert "off_session" not in create.call_args.kwargs
        webhooks.process_stripe_event(
            {
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_final",
                        "metadata": {"lead_id": str(lead.pk), "kind": "final"},
                    }
                },
            }
        )

    charge = Charge.objects.get(kind=Charge.Kind.FINAL)
    assert charge.status == Charge.Status.SUCCEEDED
    assert charge.overtime_amount == Decimal("180.00")
    assert _lines(JournalEntry.Kind.OVERTIME_CAPTURED)[0][0] == Account.CASH
    assert Task.objects.get(reservation=trip, kind="overtime_invoiced").status == "done"


def test_reconcile_finishes_a_final_charge_whose_webhook_never_came():
    lead, plan, (trip,) = _order()
    _review(trip, 45)
    create_p, retrieve_p = _stripe(_intent(status="requires_payment_method"))
    with create_p, retrieve_p:
        services.open_final_intent(plan)
    Charge.objects.filter(kind=Charge.Kind.FINAL).update(
        updated_at=timezone.now() - timedelta(hours=1)
    )

    with patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()):
        assert tasks.reconcile_open_charges() == 1

    assert Charge.objects.get(kind=Charge.Kind.FINAL).status == Charge.Status.SUCCEEDED
    assert JournalEntry.objects.filter(kind=JournalEntry.Kind.OVERTIME_CAPTURED).count() == 1


def test_the_webhook_does_not_reconcile_an_inline_charge_twice():
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    _charge(lead)

    with patch.object(stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(
            {
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_final",
                        "metadata": {"lead_id": str(lead.pk), "kind": "final"},
                    }
                },
            }
        )

    retrieve.assert_not_called()
    assert JournalEntry.objects.filter(kind=JournalEntry.Kind.OVERTIME_CAPTURED).count() == 1


def test_a_failed_final_webhook_does_not_mark_the_booked_balance_failed():
    lead, plan, (trip,) = _order()
    _review(trip, 45)

    webhooks.process_stripe_event(
        {
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_x",
                    "metadata": {"lead_id": str(lead.pk), "kind": "final"},
                    "last_payment_error": {"message": "Declined"},
                }
            },
        }
    )

    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID
    lead.refresh_from_db()
    assert lead.has_alert


# --- the Final billing card ----------------------------------------------------------


def test_the_card_offers_charge_and_link_once_reviewed(accountant):
    lead, _plan, (trip,) = _order()
    _review(trip, 45)

    body = accountant.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Charge card $180.00" in body
    assert "Send payment link" in body
    assert "visa" in body.lower() and "4242" in body


def test_a_card_less_order_only_offers_the_link(accountant):
    lead, _plan, (trip,) = _order(card=False)
    _review(trip, 45)

    body = accountant.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Charge card" not in body
    assert "Send payment link" in body


def test_the_charge_endpoint_charges(accountant):
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    create_p, retrieve_p = _stripe()

    with create_p, retrieve_p:
        resp = accountant.post(reverse("trip_review_charge", args=[lead.pk]))

    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "succeeded"


def test_the_charge_endpoint_reports_a_decline(accountant):
    lead, _plan, (trip,) = _order()
    _review(trip, 45)
    declined = stripe.error.CardError("Your card was declined.", None, "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=declined):
        resp = accountant.post(reverse("trip_review_charge", args=[lead.pk]))

    assert resp.status_code == 400
    assert "declined" in resp.json()["error"]
