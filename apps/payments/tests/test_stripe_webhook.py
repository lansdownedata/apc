"""Stripe webhook — `payment_intent.succeeded` is the only success entry point.

Hosted Checkout is gone, so `checkout.session.completed` is no longer handled at all
(spec 2026-08-30 §7). The handler branches on `metadata.kind` ∈ {deposit, balance} and
no-ops on anything already reconciled — the off-session charges in `services` confirm and
post their own ledger entry inline, yet still emit this event.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from apps.leads.models import Lead
from apps.payments import ledger, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _pi_event(lead_id, *, kind="deposit", pi="pi_1", charge_id=None):
    metadata = {"lead_id": str(lead_id), "kind": kind}
    if charge_id is not None:
        metadata["charge_id"] = str(charge_id)
    return {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": pi, "metadata": metadata}},
    }


def _as_event(payload):
    """What `construct_event` really returns: a `stripe.Event`, not a dict."""
    return stripe.Event.construct_from(payload, "sk_test")


def _intent(pi="pi_1", amount=133500, metadata=None):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        metadata=metadata or {},
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


# --- deposit ----------------------------------------------------------------
def test_deposit_saves_card_marks_paid_and_books():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id))
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_brand == "visa"
    assert plan.card_last4 == "4242"
    assert plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED).exists()
    assert JournalEntry.objects.filter(
        lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED
    ).exists()


def test_deposit_capture_pushes_lead_to_la():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings") as push,
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id))
    push.assert_called_once()
    assert push.call_args.args[0].pk == plan.lead_id


# --- balance ----------------------------------------------------------------
def test_balance_kind_reconciles_as_a_balance_capture():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(
            webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent(amount=50000)
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="balance"))
    assert JournalEntry.objects.filter(
        lead=plan.lead, kind=JournalEntry.Kind.BALANCE_CAPTURED
    ).exists()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


# --- things that must not happen --------------------------------------------
def test_checkout_session_completed_is_no_longer_handled():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"lead_id": str(plan.lead_id), "kind": "deposit"},
                "payment_intent": "pi_1",
            }
        },
    }
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(event)
    retrieve.assert_not_called()
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.UNSENT
    assert plan.charges.count() == 0


def test_an_unknown_kind_is_a_no_op():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="subscription"))
    retrieve.assert_not_called()
    assert plan.charges.count() == 0


def test_a_missing_lead_id_is_a_no_op():
    event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_1", "metadata": {"kind": "deposit"}}},
    }
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(event)
    retrieve.assert_not_called()


def test_an_already_succeeded_charge_is_not_reconciled_twice():
    """`charge_balance` / `charge_saved_card` confirm off-session and settle inline — their
    success event still arrives here and must do nothing."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_inline"
    charge.status = Charge.Status.SUCCEEDED
    charge.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    ledger.post_capture(
        lead=plan.lead,
        amount=charge.amount,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        stripe_ref="pi_inline",
        memo="Balance captured",
    )
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="balance", pi="pi_inline"))
    retrieve.assert_not_called()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


def test_an_event_that_beats_the_inline_commit_finishes_that_charge():
    """APC-126: every off-session charge creates its PENDING row, then confirms at Stripe,
    then saves the intent id. Stripe can deliver `succeeded` inside that window. The event
    carries our `charge_id`, so it must finish *that* row — not mint a second BALANCE charge
    with a second ledger entry for the same money."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    assert charge.stripe_payment_intent_id == ""
    with (
        patch.object(
            webhooks.services.stripe.PaymentIntent,
            "retrieve",
            return_value=_intent(
                pi="pi_race", amount=50000, metadata={"charge_id": str(charge.pk)}
            ),
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.process_stripe_event(
            _pi_event(plan.lead_id, kind="balance", pi="pi_race", charge_id=charge.pk)
        )
    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED
    assert charge.stripe_payment_intent_id == "pi_race"
    assert plan.charges.filter(kind=Charge.Kind.BALANCE).count() == 1
    assert (
        JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.BALANCE_CAPTURED).count()
        == 1
    )


# --- failure path (unchanged) ------------------------------------------------
def test_balance_failed_marks_failed_and_alerts():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), balance_status=PaymentPlan.BalanceStatus.SCHEDULED
    )
    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_bal",
                "metadata": {"lead_id": str(plan.lead_id), "kind": "balance"},
                "last_payment_error": {"message": "Your card was declined."},
            }
        },
    }
    webhooks.process_stripe_event(event)
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.FAILED
    assert "declined" in plan.fail_reason.lower()
    plan.lead.refresh_from_db()
    assert plan.lead.has_alert is True
    from apps.notifications.models import Notification

    assert Notification.objects.filter(
        lead=plan.lead, kind=Notification.Kind.BALANCE_FAILED
    ).exists()


# --- the view ----------------------------------------------------------------
def test_view_returns_200_on_valid_signature(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch(
            "apps.payments.views.stripe.Webhook.construct_event",
            return_value=_as_event(_pi_event(plan.lead_id)),
        ),
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )
    assert resp.status_code == 200


def test_a_redelivered_event_is_handled_once(client):
    """APC-126: Stripe retries until it sees a 200, and retries can overlap. The event id is
    the key: the first delivery is recorded and handled, a second with the same id is
    acknowledged and never reaches the handler."""
    from apps.payments.models import StripeEvent

    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    event = {"id": "evt_1", **_pi_event(plan.lead_id)}
    with (
        patch("apps.payments.views.stripe.Webhook.construct_event", return_value=_as_event(event)),
        patch("apps.payments.views.webhooks.process_stripe_event") as handle,
    ):
        for _ in range(2):
            resp = client.post(
                "/webhooks/stripe/",
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
            )
            assert resp.status_code == 200
    handle.assert_called_once()
    (logged,) = StripeEvent.objects.filter(event_id="evt_1")
    assert logged.event_type == "payment_intent.succeeded"
    assert logged.processed is True


def test_la_push_crash_never_breaks_stripe_200(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch(
            "apps.payments.views.stripe.Webhook.construct_event",
            return_value=_as_event(_pi_event(plan.lead_id)),
        ),
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch(
            "apps.integrations.la_sync.push_lead_bookings",
            side_effect=RuntimeError("boom"),
        ),
    ):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )
    assert resp.status_code == 200


# --- real stripe-python objects ----------------------------------------------------------
# stripe-python 15 objects are not dicts: `.get`, `dict(obj)` and iteration raise. Every
# test above hands the handler plain dicts and MagicMocks, so none of them could see it.
# These sign a real payload with a test secret, so `construct_event` itself runs and returns
# a genuine `stripe.Event`, and Stripe's reads come back as genuine `stripe.PaymentIntent`s.

WHSEC = "whsec_test_real_objects"


def _signed_post(client, settings, etype, obj, *, evt="evt_real_1"):
    settings.STRIPE_WEBHOOK_SECRET = WHSEC
    body = json.dumps(
        {
            "id": evt,
            "object": "event",
            "type": etype,
            "livemode": False,
            "created": 1,
            "data": {"object": {"object": "payment_intent", **obj}},
        }
    ).encode()
    stamp = int(time.time())
    sig = hmac.new(WHSEC.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/stripe/",
        data=body,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={stamp},v1={sig}",
    )


def _real_intent(pi, *, status, amount, metadata):
    return stripe.PaymentIntent.construct_from(
        {
            "id": pi,
            "object": "payment_intent",
            "status": status,
            "amount": amount,
            "amount_received": amount if status == "succeeded" else 0,
            "metadata": metadata,
            "payment_method": {
                "id": "pm_1",
                "object": "payment_method",
                "customer": None,
                "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
            },
        },
        "sk_test_real_objects",
    )


def _logged(evt="evt_real_1"):
    from apps.payments.models import StripeEvent

    return StripeEvent.objects.get(event_id=evt)


def test_real_event_canceled_hold_is_logged_processed_and_expires_the_hold(client, settings):
    """APC-95's path: the bank drops a cleaning hold."""
    plan = PaymentPlanFactory(lead__status=Lead.Status.BOOKED)
    hold = plan.charges.create(
        kind=Charge.Kind.HOLD,
        amount=Decimal("500.00"),
        status=Charge.Status.AUTHORIZED,
        stripe_payment_intent_id="pi_hold",
        idempotency_key=f"hold-{plan.pk}",
    )
    metadata = {"lead_id": str(plan.lead_id), "kind": "hold", "charge_id": str(hold.pk)}

    resp = _signed_post(
        client,
        settings,
        "payment_intent.canceled",
        {"id": "pi_hold", "status": "canceled", "metadata": metadata},
    )

    assert resp.status_code == 200
    logged = _logged()
    assert logged.processed is True
    assert logged.event_type == "payment_intent.canceled"
    assert logged.payload["data"]["object"]["metadata"] == metadata
    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED


def test_real_event_amount_capturable_updated_records_the_deposit_hold(client, settings):
    """APC-26's path: the only signal that the customer paid at checkout."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.status = Lead.Status.QUOTED
    plan.lead.save(update_fields=["status"])
    metadata = {"lead_id": str(plan.lead_id), "kind": "deposit"}
    intent = _real_intent("pi_dep", status="requires_capture", amount=133500, metadata=metadata)

    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=intent):
        resp = _signed_post(
            client,
            settings,
            "payment_intent.amount_capturable_updated",
            {"id": "pi_dep", "status": "requires_capture", "metadata": metadata},
        )

    assert resp.status_code == 200
    assert _logged().processed is True
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.AUTHORIZED
    assert plan.lead.status == Lead.Status.ENGAGED
    assert plan.card_last4 == "4242"


def test_real_event_succeeded_books_the_deposit(client, settings):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    metadata = {"lead_id": str(plan.lead_id), "kind": "deposit"}
    intent = _real_intent("pi_paid", status="succeeded", amount=133500, metadata=metadata)

    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        resp = _signed_post(
            client,
            settings,
            "payment_intent.succeeded",
            {"id": "pi_paid", "status": "succeeded", "metadata": metadata},
        )

    assert resp.status_code == 200
    assert _logged().processed is True
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED).exists()


def test_a_real_intent_still_finds_its_charge_by_metadata_charge_id():
    """APC-126's race guard, read from a genuine `stripe.PaymentIntent`. Its `metadata` is a
    StripeObject, not a dict; read as `{}` the `charge_id` is lost and `record_payment` (the
    reconcile cron, the inline off-session charges, the webhook) mints a second charge and a
    second ledger entry for the same money."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    intent = _real_intent(
        "pi_race",
        status="succeeded",
        amount=50000,
        metadata={"lead_id": str(plan.lead_id), "kind": "balance", "charge_id": str(charge.pk)},
    )

    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.services.record_payment(plan, "pi_race", kind=Charge.Kind.BALANCE)

    charge.refresh_from_db()
    assert charge.stripe_payment_intent_id == "pi_race"
    assert charge.status == Charge.Status.SUCCEEDED
    assert plan.charges.filter(kind=Charge.Kind.BALANCE).count() == 1


def test_view_returns_400_on_bad_signature(client):
    err = stripe.error.SignatureVerificationError("bad sig", "sig")
    with patch("apps.payments.views.stripe.Webhook.construct_event", side_effect=err):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="x",
        )
    assert resp.status_code == 400


def test_a_declined_deposit_attempt_is_not_a_balance_failure():
    """PAY-03: the customer is on the pay page when a deposit attempt fails and simply tries
    again. The attempt's row is marked failed, but the balance, the lead's alert and the
    staff bell are left alone."""
    from apps.notifications.models import Notification

    plan = PaymentPlanFactory(quote_total=Decimal("115.00"))
    before = plan.balance_status
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("57.50"))
    charge.stripe_payment_intent_id = "pi_dep"
    charge.save(update_fields=["stripe_payment_intent_id"])

    webhooks.process_stripe_event(
        {
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_dep",
                    "metadata": {"lead_id": str(plan.lead_id), "kind": "deposit"},
                    "last_payment_error": {
                        "message": "The provided PaymentMethod has failed authentication."
                    },
                }
            },
        }
    )

    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    charge.refresh_from_db()
    assert charge.status == Charge.Status.FAILED
    assert plan.balance_status == before
    assert plan.fail_reason == ""
    assert plan.lead.has_alert is False
    assert not Notification.objects.filter(lead=plan.lead).exists()
