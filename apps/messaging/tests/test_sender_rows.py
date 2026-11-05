"""The sender reads the edited message rows (moved from spec B's `test_overrides.py` when
`messaging.overrides` was deleted, APC-119; the tests are unchanged)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.messaging import sender
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint

pytestmark = pytest.mark.django_db


def test_send_uses_edited_text(settings):
    settings.TOUCHPOINTS_ENABLED = True
    MessageTemplate.objects.filter(key="order_confirmed").update(sms_body="Booked {quote_no}")
    lead = LeadFactory(contact=ContactFactory(email="", phone="5551234567"))
    TouchPointFactory(
        lead=lead,
        kind=TouchPoint.Kind.ORDER_CONFIRMED,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )

    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}) as mock_send:
        sender.run_touchpoints()

    bodies = [c.kwargs["body"] for c in mock_send.call_args_list]
    assert bodies == [f"Booked {lead.quote_no}"]


def test_rows_load_in_two_queries_per_run(settings):
    """Two row queries per run, however many sends are due (Moe accepted 1 -> 2, 2026-11-05)."""
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(contact=ContactFactory(email="a@example.com", phone="5551234567"))
    for kind in ("tp1_welcome", "tp2_lead_followup", "order_confirmed"):
        TouchPointFactory(lead=lead, kind=kind, scheduled_for=timezone.now() - timedelta(minutes=1))

    with (
        patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}),
        CaptureQueriesContext(connection) as ctx,
    ):
        sender.run_touchpoints()

    hits = [q for q in ctx.captured_queries if "messaging_messagetemplate" in q["sql"]]
    assert len(hits) == 2
