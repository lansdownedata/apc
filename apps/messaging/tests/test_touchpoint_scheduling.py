from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import TouchPoint
from apps.payments.factories import PaymentPlanFactory
from apps.payments.webhooks import process_stripe_event

pytestmark = pytest.mark.django_db


def test_schedule_lead_created_makes_tp1_and_tp2():
    lead = LeadFactory()
    before = timezone.now()
    touchpoints.schedule_lead_created(lead)
    after = timezone.now()

    tp1 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)
    tp2 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP2_LEAD_FOLLOWUP)
    assert tp1.status == TouchPoint.Status.SCHEDULED
    assert before + timedelta(minutes=30) <= tp1.scheduled_for <= after + timedelta(minutes=30)
    assert before + timedelta(hours=2) <= tp2.scheduled_for <= after + timedelta(hours=2)


def test_schedule_quote_sent_makes_tp3_tp6_tp7_tp8():
    sent = timezone.now()
    expires = sent + timedelta(days=3)
    lead = LeadFactory(quote_sent_at=sent, quote_expires_at=expires)

    touchpoints.schedule_quote_sent(lead)

    tp3 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)
    tp6 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP6_QUOTE_FOLLOWUP)
    tp7 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP7_EXPIRING)
    tp8 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP8_EXPIRED)

    assert tp3.scheduled_for == sent + timedelta(minutes=3)
    assert tp6.scheduled_for == sent + timedelta(hours=24)
    assert tp7.scheduled_for == expires - timedelta(hours=24)
    assert tp8.scheduled_for == expires + timedelta(hours=24)


def test_schedule_quote_sent_resend_cancels_old_and_recreates():
    sent = timezone.now()
    expires = sent + timedelta(days=3)
    lead = LeadFactory(quote_sent_at=sent, quote_expires_at=expires)

    touchpoints.schedule_quote_sent(lead)
    old_tp3 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)

    new_sent = sent + timedelta(hours=1)
    new_expires = expires + timedelta(days=1)
    lead.quote_sent_at = new_sent
    lead.quote_expires_at = new_expires
    lead.save(update_fields=["quote_sent_at", "quote_expires_at"])

    touchpoints.schedule_quote_sent(lead)

    old_tp3.refresh_from_db()
    assert old_tp3.status == TouchPoint.Status.CANCELLED

    new_tp3 = (
        TouchPoint.objects.filter(
            lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS, status=TouchPoint.Status.SCHEDULED
        )
        .exclude(pk=old_tp3.pk)
        .get()
    )
    assert new_tp3.scheduled_for == new_sent + timedelta(minutes=3)


def test_schedule_quote_sent_skips_tp7_when_expiry_already_within_24h():
    sent = timezone.now()
    expires = timezone.now() + timedelta(hours=1)  # expires - 24h is in the past
    lead = LeadFactory(quote_sent_at=sent, quote_expires_at=expires)

    touchpoints.schedule_quote_sent(lead)

    assert not TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP7_EXPIRING).exists()
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP8_EXPIRED).exists()


def test_schedule_quote_viewed_creates_tp4_tp5_once():
    lead = LeadFactory()
    touchpoints.schedule_quote_viewed(lead)
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP4_VIEWED_SMS).count() == 1
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP5_VIEWED_EMAIL).count() == 1

    touchpoints.schedule_quote_viewed(lead)
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP4_VIEWED_SMS).count() == 1
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP5_VIEWED_EMAIL).count() == 1


def test_schedule_quote_viewed_reschedules_after_cancellation():
    lead = LeadFactory()
    touchpoints.schedule_quote_viewed(lead)
    old_tp4 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP4_VIEWED_SMS)
    old_tp5 = TouchPoint.objects.get(lead=lead, kind=TouchPoint.Kind.TP5_VIEWED_EMAIL)

    # A quote re-send cancels the pending viewed nudges (among others).
    touchpoints.schedule_quote_sent(lead)
    old_tp4.refresh_from_db()
    old_tp5.refresh_from_db()
    assert old_tp4.status == TouchPoint.Status.CANCELLED
    assert old_tp5.status == TouchPoint.Status.CANCELLED

    touchpoints.schedule_quote_viewed(lead)

    fresh_tp4 = (
        TouchPoint.objects.filter(
            lead=lead, kind=TouchPoint.Kind.TP4_VIEWED_SMS, status=TouchPoint.Status.SCHEDULED
        )
        .exclude(pk=old_tp4.pk)
        .get()
    )
    fresh_tp5 = (
        TouchPoint.objects.filter(
            lead=lead, kind=TouchPoint.Kind.TP5_VIEWED_EMAIL, status=TouchPoint.Status.SCHEDULED
        )
        .exclude(pk=old_tp5.pk)
        .get()
    )
    assert fresh_tp4 and fresh_tp5


def test_schedule_review_request_idempotent():
    lead = LeadFactory()
    touchpoints.schedule_review_request(lead)
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST).count() == 1

    touchpoints.schedule_review_request(lead)
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST).count() == 1


def test_cancel_pending_only_cancels_scheduled_and_spares_review_request_by_default():
    lead = LeadFactory()
    scheduled = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.TP1_WELCOME, status=TouchPoint.Status.SCHEDULED
    )
    sent = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.TP2_LEAD_FOLLOWUP, status=TouchPoint.Status.SENT
    )
    review = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST, status=TouchPoint.Status.SCHEDULED
    )

    touchpoints.cancel_pending(lead)

    scheduled.refresh_from_db()
    sent.refresh_from_db()
    review.refresh_from_db()
    assert scheduled.status == TouchPoint.Status.CANCELLED
    assert sent.status == TouchPoint.Status.SENT
    assert review.status == TouchPoint.Status.SCHEDULED


def test_cancel_pending_with_explicit_kinds_including_review_request():
    lead = LeadFactory()
    review = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST, status=TouchPoint.Status.SCHEDULED
    )

    touchpoints.cancel_pending(lead, kinds=list(TouchPoint.Kind.values))

    review.refresh_from_db()
    assert review.status == TouchPoint.Status.CANCELLED


# --- Wiring tests ---


def test_lead_create_view_schedules_tp1_and_tp2(client):
    client.force_login(UserFactory())
    resp = client.post(
        reverse("lead_create"),
        {
            "name": "Sarah Boyne",
            "company": "",
            "phone": "(703) 555-0148",
            "email": "sarah@example.com",
            "channel": "phone",
            "agent": "",
        },
    )
    assert resp.status_code == 302
    lead = Lead.objects.get()
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME).exists()
    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.TP2_LEAD_FOLLOWUP).exists()


def test_mark_lost_cancels_all_kinds_including_review_request(client):
    lead = LeadFactory(status=Lead.Status.NEW)
    review = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST, status=TouchPoint.Status.SCHEDULED
    )
    welcome = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.TP1_WELCOME, status=TouchPoint.Status.SCHEDULED
    )
    client.force_login(UserFactory())
    client.post(reverse("lead_mark_lost", args=[lead.pk]), {"reason": "Booked elsewhere"})

    review.refresh_from_db()
    welcome.refresh_from_db()
    assert review.status == TouchPoint.Status.CANCELLED
    assert welcome.status == TouchPoint.Status.CANCELLED


def test_deposit_webhook_cancels_pending_touchpoints():
    plan = PaymentPlanFactory()
    from apps.payments.tests.test_stripe_webhook import _intent, _pi_event

    with (
        patch("apps.payments.services.stripe.PaymentIntent.retrieve", return_value=_intent()),
        patch("apps.leads.services.touchpoints.cancel_pending") as cancel_pending,
    ):
        process_stripe_event(_pi_event(plan.lead_id))

    cancel_pending.assert_called_once()
    assert cancel_pending.call_args.args[0].pk == plan.lead_id


def test_podium_inbound_message_schedules_no_touchpoints():
    """Inverted deliberately (spec 2026-07-28).

    This used to assert that an inbound text scheduled TP1/TP2. It runs on the main
    business number, so that sent a wrong number the "thank you for visiting our
    website" welcome by SMS *and* email 30 minutes later. Touch-points now start only
    when an agent qualifies the conversation into a lead.
    """
    from apps.integrations.webhooks import process_podium_webhook

    payload = {
        "eventType": "message.received",
        "data": {
            "uid": "msg-1",
            "body": "Hi there",
            "contact": {"uid": "c-1", "name": "New Person", "phoneNumber": "+15551234567"},
            "conversation": {"uid": "conv-1", "channel": {"type": "phone", "identifier": "+1"}},
        },
    }
    event = process_podium_webhook(payload)

    assert event.conversation is not None
    assert TouchPoint.objects.count() == 0
