"""Template registry completeness + context building from real lead data."""

from datetime import timedelta

import pytest

from apps.leads.factories import LeadFactory
from apps.messaging import touchpoint_templates as tpl
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory, StopFactory

pytestmark = pytest.mark.django_db


def test_registry_covers_every_kind():
    assert set(tpl.TEMPLATES) == set(TouchPoint.Kind.values)


def test_offsets_and_channels_match_client_cadence():
    t = tpl.TEMPLATES
    assert t["tp1_welcome"].offset == timedelta(minutes=30)
    assert t["tp1_welcome"].channels == ("email", "sms")
    assert t["tp2_lead_followup"].offset == timedelta(hours=2)
    assert t["tp3_quote_sent_sms"].offset == timedelta(minutes=3)
    assert t["tp4_viewed_sms"].offset == timedelta(minutes=20)
    assert t["tp5_viewed_email"].offset == timedelta(hours=2)
    assert t["tp6_quote_followup"].offset == timedelta(hours=24)
    assert t["tp7_expiring"].offset == timedelta(hours=-24)
    assert t["tp7_expiring"].anchor == "quote_expires"
    assert t["tp8_expired"].offset == timedelta(hours=24)


def test_every_template_renders_with_context(settings):
    settings.COMPANY_PHONE = "(202) 424-2600"
    lead = LeadFactory(contact__name="Jane Doe")
    res = ReservationFactory(lead=lead)
    StopFactory(reservation=res, sequence=0, address="JFK Airport")
    ctx = tpl.build_context(lead)
    ctx["quote_link"] = "https://x/quote/tok/"
    ctx["review_link"] = "https://x/rev"
    for template in tpl.TEMPLATES.values():
        for text in (template.subject, template.email_body, template.sms_body):
            tpl.render(text, ctx)  # KeyError => missing context key


def test_context_values():
    lead = LeadFactory(contact__name="Jane Doe")
    ctx = tpl.build_context(lead)
    assert ctx["first_name"] == "Jane"
    assert ctx["quote_no"] == lead.quote_no
