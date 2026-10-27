from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.messaging import overrides, touchpoints
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING

pytestmark = pytest.mark.django_db


def test_no_row_is_code_default():
    assert overrides.effective("tp1_welcome") == TEMPLATES["tp1_welcome"]
    assert overrides.effective("thank_you_wedding") == THANK_YOU_WEDDING


def test_row_overrides_only_non_blank():
    MessageTemplate.objects.create(kind="tp1_welcome", sms_body="Hi {first_name}")
    base = TEMPLATES["tp1_welcome"]

    got = overrides.effective("tp1_welcome")

    assert got.sms_body == "Hi {first_name}"
    assert got.subject == base.subject
    assert got.email_body == base.email_body
    assert got.offset == base.offset


def test_timing_row_keeps_sign():
    MessageTemplate.objects.create(kind="tp7_expiring", timing_amount=2, timing_unit="days")
    MessageTemplate.objects.create(kind="tp1_welcome", timing_amount=45, timing_unit="minutes")

    assert overrides.effective("tp7_expiring").offset == -timedelta(days=2)
    assert overrides.effective("tp1_welcome").offset == timedelta(minutes=45)


def test_timing_of_defaults():
    assert overrides.timing_of("tp1_welcome") == (30, "minutes")
    assert overrides.timing_of("tp2_lead_followup") == (2, "hours")
    assert overrides.timing_of("tp6_quote_followup") == (1, "days")
    assert overrides.timing_of("payment_reminder") == (3, "days")
    assert overrides.timing_of("driver_released") == (12, "hours")
    assert overrides.timing_of("order_confirmed") is None


def test_timing_of_uses_row():
    MessageTemplate.objects.create(kind="tp1_welcome", timing_amount=2, timing_unit="hours")
    assert overrides.timing_of("tp1_welcome") == (2, "hours")


def test_wedding_thank_you_is_its_own_row():
    MessageTemplate.objects.create(kind="thank_you_wedding", email_body="WEDDING BODY")
    wedding = LeadFactory(wedding_name="Smith")
    plain = LeadFactory(wedding_name="")

    assert touchpoints._thank_you_template(wedding).email_body == "WEDDING BODY"
    assert touchpoints._thank_you_template(plain) == TEMPLATES["thank_you"]


def test_scheduling_uses_effective_offset():
    MessageTemplate.objects.create(kind="tp2_lead_followup", timing_amount=5, timing_unit="hours")
    lead = LeadFactory()

    touchpoints.schedule_lead_created(lead)

    tp = TouchPoint.objects.get(lead=lead, kind="tp2_lead_followup")
    tp1 = TouchPoint.objects.get(lead=lead, kind="tp1_welcome")
    assert tp.scheduled_for - tp1.scheduled_for == timedelta(hours=5) - timedelta(minutes=30)


def test_send_uses_edited_text(settings):
    settings.TOUCHPOINTS_ENABLED = True
    MessageTemplate.objects.create(kind="order_confirmed", sms_body="Booked {quote_no}")
    lead = LeadFactory(contact=ContactFactory(email="", phone="5551234567"))
    TouchPointFactory(
        lead=lead,
        kind=TouchPoint.Kind.ORDER_CONFIRMED,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )

    with patch(
        "apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}
    ) as mock_send:
        touchpoints.run_touchpoints()

    bodies = [c.kwargs["body"] for c in mock_send.call_args_list]
    assert bodies == [f"Booked {lead.quote_no}"]


def test_rows_load_once_per_run(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(contact=ContactFactory(email="a@example.com", phone="5551234567"))
    for kind in ("tp1_welcome", "tp2_lead_followup", "order_confirmed"):
        TouchPointFactory(lead=lead, kind=kind, scheduled_for=timezone.now() - timedelta(minutes=1))

    with (
        patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}),
        CaptureQueriesContext(connection) as ctx,
    ):
        touchpoints.run_touchpoints()

    hits = [q for q in ctx.captured_queries if "messaging_messagetemplate" in q["sql"]]
    assert len(hits) == 1
