"""The 72-hour pre-charge notice (spec 2026-08-30 §9).

Not general dunning: it exists because `charge-due-balances` takes the balance off-session on
`balance_due_date`, and the customer should be told before that happens. So it only fires for a
BOOKED lead with a card on file and the balance scheduled — the exact case every TP1–TP8 kind
skips. Unpaid deposits are the office's job (the deposit report + Send payment link).
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.models import TouchPoint
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

PICKUP = date(2026, 12, 1)  # balance due 2026-11-01, reminder 2026-10-29 00:00


def _plan(**kwargs):
    kwargs.setdefault("quote_total", "1000.00")
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_payment_method_id", "pm_1")
    kwargs.setdefault("card_last4", "4242")
    kwargs.setdefault("balance_status", PaymentPlan.BalanceStatus.SCHEDULED)
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    plan = PaymentPlanFactory(lead=lead, **kwargs)
    ReservationFactory(lead=lead, pickup_date=PICKUP)
    return plan


def _reminder(lead):
    return TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.PAYMENT_REMINDER)


# --- scheduling -----------------------------------------------------------
def test_scheduled_when_the_balance_flips_to_scheduled():
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    assert _reminder(plan.lead).count() == 1


def test_scheduling_is_idempotent():
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    touchpoints.schedule_payment_reminder(plan.lead)
    assert _reminder(plan.lead).count() == 1


def test_anchor_is_72_hours_before_the_balance_due_date():
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    tp = _reminder(plan.lead).get()
    expected = timezone.make_aware(
        datetime.combine(PICKUP - timedelta(days=30), time.min),
        timezone.get_default_timezone(),
    ) - timedelta(hours=72)
    assert tp.scheduled_for == expected


def test_not_scheduled_for_a_card_less_booking():
    plan = _plan(stripe_payment_method_id="", card_last4="")
    touchpoints.schedule_payment_reminder(plan.lead)
    assert not _reminder(plan.lead).exists()


def test_not_scheduled_without_a_dated_trip():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    PaymentPlanFactory(
        lead=lead,
        quote_total="1000.00",
        stripe_payment_method_id="pm_1",
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
    )
    ReservationFactory(lead=lead, pickup_date=None)
    touchpoints.schedule_payment_reminder(lead)
    assert not _reminder(lead).exists()


def test_sync_plan_from_collected_schedules_it_when_the_balance_becomes_scheduled():
    plan = _plan(balance_status=PaymentPlan.BalanceStatus.NA)
    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    ledger.post_capture(
        lead=plan.lead,
        amount=plan.deposit_amount,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="seed-dep",
    )
    services.sync_plan_from_collected(plan)
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED
    assert _reminder(plan.lead).count() == 1


# --- sending -------------------------------------------------------------
def _due(tp):
    tp.scheduled_for = timezone.now() - timedelta(minutes=1)
    tp.save(update_fields=["scheduled_for"])


def test_sends_for_a_booked_lead_with_a_card_and_the_balance_scheduled(settings):
    settings.PUBLIC_BASE_URL = "https://pay.example.com"
    plan = _plan()
    plan.lead.contact.phone = "+15715551212"
    plan.lead.contact.save()
    touchpoints.schedule_payment_reminder(plan.lead)
    tp = _reminder(plan.lead).get()
    _due(tp)
    send_path = "apps.messaging.touchpoints.podium.send_message"
    with patch(send_path, return_value={"uid": "m1"}) as send:
        assert touchpoints._process(tp) is True
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    body = send.call_args.kwargs["body"]
    assert "https://pay.example.com/quote/" in body
    assert "4242" in body


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda p: setattr_and_save(p.lead, "status", Lead.Status.LOST), "LOST"),
        (lambda p: setattr_and_save(p, "stripe_payment_method_id", ""), "no card"),
        (
            lambda p: setattr_and_save(p, "balance_status", PaymentPlan.BalanceStatus.PAID),
            "balance not scheduled",
        ),
    ],
)
def test_skips(settings, mutate, reason):
    settings.PUBLIC_BASE_URL = "https://pay.example.com"
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    tp = _reminder(plan.lead).get()
    _due(tp)
    mutate(plan)
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert touchpoints._process(tp) is False
    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED


def test_skips_when_nothing_is_owed(settings):
    settings.PUBLIC_BASE_URL = "https://pay.example.com"
    plan = _plan()
    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    ledger.post_capture(
        lead=plan.lead,
        amount=plan.quote_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="seed-full",
    )
    touchpoints.schedule_payment_reminder(plan.lead)
    tp = _reminder(plan.lead).get()
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert touchpoints._process(tp) is False
    send.assert_not_called()


def test_left_scheduled_when_public_base_url_is_unset(settings):
    settings.PUBLIC_BASE_URL = ""
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    tp = _reminder(plan.lead).get()
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        assert touchpoints._process(tp) is False
    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED


# --- cancel_pending leaves it alone ------------------------------------------
def test_cancel_pending_default_does_not_cancel_the_reminder():
    plan = _plan()
    touchpoints.schedule_payment_reminder(plan.lead)
    touchpoints.cancel_pending(plan.lead)
    tp = _reminder(plan.lead).get()
    assert tp.status == TouchPoint.Status.SCHEDULED


def setattr_and_save(obj, field, value):
    setattr(obj, field, value)
    obj.save(update_fields=[field, "updated_at"])
