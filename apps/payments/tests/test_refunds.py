from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.dispatch import services as dispatch_services
from apps.dispatch.models import Assignment
from apps.leads.models import Lead
from apps.payments import ledger, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations.factories import TransferReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _paid_plan():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=Decimal("1335.00"),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_bal",
        idempotency_key="seed-bal",
    )
    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="cap-bal",
    )
    return plan


def test_refund_payment_posts_stripe_and_ledger():
    plan = _paid_plan()
    with patch.object(services.stripe.Refund, "create", return_value=MagicMock(id="re_1")):
        refunded = services.refund_payment(plan, Decimal("400.00"))
    assert refunded == Decimal("400.00")
    assert plan.charges.filter(kind=Charge.Kind.REFUND, stripe_refund_id="re_1").exists()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("935.00")


def test_refund_endpoint_requires_payment_access(client):
    plan = _paid_plan()
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    resp = client.post(reverse("order_refund", args=[plan.lead_id]), {"amount": "100.00"})
    assert resp.status_code == 403


def test_cancel_and_refund_cancels_and_reverses(client):
    plan = _paid_plan()
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status", "updated_at"])
    res = TransferReservationFactory(lead=plan.lead, rate=Decimal("1335.00"))
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch.object(services.stripe.Refund, "create", return_value=MagicMock(id="re_2")):
        resp = client.post(reverse("order_cancel_refund", args=[plan.lead_id]))
    assert resp.status_code in (200, 302)
    plan.lead.refresh_from_db()
    res.refresh_from_db()
    assert plan.lead.status == Lead.Status.LOST
    assert res.revenue_status == res.RevenueStatus.REVERSED


def test_cancel_and_refund_withdraws_affiliate_coverage(client):
    """The board hides cancelled trips, and nothing lists assignments by vendor — so an
    offer left active here is one no dispatcher can ever find, while the affiliate still
    thinks he is covering the trip."""
    plan = _paid_plan()
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status", "updated_at"])
    res = TransferReservationFactory(lead=plan.lead, rate=Decimal("1335.00"))
    offer = dispatch_services.send_offer(res, VendorFactory(), payout=Decimal("900.00"))

    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch.object(services.stripe.Refund, "create", return_value=MagicMock(id="re_3")):
        client.post(reverse("order_cancel_refund", args=[plan.lead_id]))

    offer.refresh_from_db()
    assert offer.status == Assignment.Status.WITHDRAWN
    assert offer.note == "Order cancelled"


def test_refund_caps_at_captured_no_double_refund():
    plan = _paid_plan()  # one succeeded balance charge of 1335 on pi_bal
    with patch.object(
        services.stripe.Refund,
        "create",
        side_effect=[MagicMock(id="re_a"), MagicMock(id="re_b")],
    ):
        first = services.refund_payment(plan, Decimal("1335.00"))
        second = services.refund_payment(plan, Decimal("1335.00"))
    assert first == Decimal("1335.00")
    assert second == Decimal("0.00")  # already fully refunded — no second Stripe refund
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("0.00")  # never negative


def test_refund_endpoint_rejects_bad_amount(client):
    plan = _paid_plan()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(reverse("order_refund", args=[plan.lead_id]), {"amount": "abc"})
    assert resp.status_code in (200, 302)  # graceful, not a 500


def test_refund_passes_stripe_idempotency_key():
    plan = _paid_plan()
    mock_create = MagicMock(return_value=MagicMock(id="re_k"))
    with patch.object(services.stripe.Refund, "create", mock_create):
        services.refund_payment(plan, Decimal("400.00"))
    assert "idempotency_key" in mock_create.call_args.kwargs
    assert mock_create.call_args.kwargs["idempotency_key"].startswith("refund-")


def test_cancel_refund_only_booked(client):
    from apps.leads.models import Lead

    plan = PaymentPlanFactory(quote_total=Decimal("100.00"))  # lead defaults to NEW
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(reverse("order_cancel_refund", args=[plan.lead_id]))
    assert resp.status_code in (200, 302)
    plan.lead.refresh_from_db()
    assert plan.lead.status != Lead.Status.LOST  # not booked → no cancel


# --- the final bill (APC-75) ------------------------------------------------------------
# Moe, 2026-10-24: newest money back first (FINAL, BALANCE, DEPOSIT); inside a FINAL
# charge, overtime first (gratuity, then base, then the charge's balance part).


def _charge(plan, kind, amount, pi, **extra):
    return plan.charges.create(
        kind=kind,
        amount=Decimal(amount),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id=pi,
        idempotency_key=f"seed-{pi}",
        **extra,
    )


def _fully_paid_with_final():
    """Deposit $500 + balance $500 + a $260 final bill ($100 balance part, $160 overtime of
    which $40 is gratuity), all posted the way the real flows post them."""
    plan = PaymentPlanFactory(quote_total=Decimal("1100.00"))
    lead = plan.lead
    for kind, pi, entry in (
        (Charge.Kind.DEPOSIT, "pi_dep", JournalEntry.Kind.DEPOSIT_CAPTURED),
        (Charge.Kind.BALANCE, "pi_bal", JournalEntry.Kind.BALANCE_CAPTURED),
    ):
        charge = _charge(plan, kind, "500.00", pi)
        ledger.post_capture(
            lead=lead,
            amount=Decimal("500.00"),
            kind=entry,
            idempotency_key=f"cap-{pi}",
            charge=charge,
        )
    final = _charge(
        plan,
        Charge.Kind.FINAL,
        "260.00",
        "pi_fin",
        overtime_amount=Decimal("160.00"),
        overtime_gratuity=Decimal("40.00"),
    )
    services._post_final(plan, final, "pi_fin")
    return plan


def _stripe_refunds(*ids):
    return patch.object(
        services.stripe.Refund, "create", side_effect=[MagicMock(id=i) for i in ids]
    )


def _all_balanced(lead):
    return all(e.is_balanced for e in JournalEntry.objects.filter(lead=lead))


def _recognized(lead):
    return ledger.order_balances(lead)["recognized"]


def test_refundable_total_includes_the_final_bill():
    plan = _fully_paid_with_final()

    assert services.refundable_total(plan) == Decimal("1260.00")


def test_a_full_refund_returns_every_dollar_newest_first():
    plan = _fully_paid_with_final()

    with _stripe_refunds("re_f", "re_b", "re_d") as create:
        refunded = services.refund_payment(plan, Decimal("1260.00"))

    assert refunded == Decimal("1260.00")
    pis = [c.kwargs["payment_intent"] for c in create.call_args_list]
    assert pis == ["pi_fin", "pi_bal", "pi_dep"]
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("0.00")
    assert _all_balanced(plan.lead)


def test_a_small_refund_comes_off_the_overtime_gratuity_first():
    plan = _fully_paid_with_final()
    before = _recognized(plan.lead)

    with _stripe_refunds("re_1") as create:
        services.refund_payment(plan, Decimal("30.00"))

    assert create.call_args.kwargs["payment_intent"] == "pi_fin"
    # Overtime reverses its Recognized Revenue lines; deferred revenue is not touched.
    assert _recognized(plan.lead) == before - Decimal("30.00")
    assert ledger.order_balances(plan.lead)["deferred"] == Decimal("1100.00")
    assert _all_balanced(plan.lead)


def test_a_second_partial_continues_where_the_first_stopped():
    plan = _fully_paid_with_final()
    before = _recognized(plan.lead)

    with _stripe_refunds("re_1", "re_2"):
        services.refund_payment(plan, Decimal("30.00"))  # $30 of the $40 gratuity
        services.refund_payment(plan, Decimal("150.00"))  # $10 gratuity, $120 base, $20 balance

    assert _recognized(plan.lead) == before - Decimal("160.00")  # all overtime reversed
    assert ledger.order_balances(plan.lead)["deferred"] == Decimal("1080.00")  # $20 balance part
    assert _all_balanced(plan.lead)


def test_a_refund_spanning_the_final_bill_and_the_balance():
    plan = _fully_paid_with_final()

    with _stripe_refunds("re_f", "re_b") as create:
        refunded = services.refund_payment(plan, Decimal("400.00"))

    amounts = [c.kwargs["amount"] for c in create.call_args_list]
    assert refunded == Decimal("400.00")
    assert amounts == [26000, 14000]  # all $260 of the final bill, then $140 of the balance
    assert _all_balanced(plan.lead)


def test_a_final_bill_with_no_overtime_refunds_like_a_balance():
    plan = PaymentPlanFactory(quote_total=Decimal("100.00"))
    final = _charge(plan, Charge.Kind.FINAL, "100.00", "pi_fin")
    services._post_final(plan, final, "pi_fin")
    before = _recognized(plan.lead)

    with _stripe_refunds("re_1"):
        services.refund_payment(plan, Decimal("100.00"))

    assert _recognized(plan.lead) == before
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("0.00")
    assert _all_balanced(plan.lead)


def test_a_retried_final_refund_makes_one_stripe_call():
    plan = _fully_paid_with_final()

    with _stripe_refunds("re_1", "re_2") as create:
        services.refund_payment(plan, Decimal("260.00"))
        services.refund_payment(plan, Decimal("260.00"))  # FINAL is exhausted; moves on

    first, second = create.call_args_list
    assert first.kwargs["payment_intent"] == "pi_fin"
    assert second.kwargs["payment_intent"] == "pi_bal"
    assert (
        plan.charges.filter(kind=Charge.Kind.REFUND, stripe_payment_intent_id="pi_fin").count() == 1
    )


def test_refunding_overtime_never_reopens_overtime_invoiced():
    from django.utils import timezone

    from apps.tasks.models import Task

    plan = _fully_paid_with_final()
    task = Task.objects.create(
        kind="overtime_invoiced",
        lead=plan.lead,
        department="accounting",
        status=Task.Status.DONE,
        opens_at=timezone.now(),
    )

    with _stripe_refunds("re_1"):
        services.refund_payment(plan, Decimal("160.00"))

    task.refresh_from_db()
    assert task.status == Task.Status.DONE


def test_an_over_refund_is_refused_with_a_message(client):
    plan = _fully_paid_with_final()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    with patch.object(services.stripe.Refund, "create") as create:
        resp = client.post(
            reverse("order_refund", args=[plan.lead_id]), {"amount": "1260.01"}, follow=True
        )

    create.assert_not_called()
    assert "at most $1,260.00" in resp.content.decode()


def test_cancel_and_refund_returns_the_final_bill_too(client):
    plan = _fully_paid_with_final()
    Lead.objects.filter(pk=plan.lead_id).update(status=Lead.Status.BOOKED)
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    with _stripe_refunds("re_f", "re_b", "re_d"):
        client.post(reverse("order_cancel_refund", args=[plan.lead_id]))

    assert ledger.order_balances(plan.lead)["collected"] == Decimal("0.00")
