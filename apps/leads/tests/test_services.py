"""`leads.services.mark_lost`: the one path a lead takes to Lost."""

import pytest

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import TouchPoint

pytestmark = pytest.mark.django_db


def test_mark_lost_sets_reason_and_cancels_touchpoints():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    review = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST, status=TouchPoint.Status.SCHEDULED
    )
    welcome = TouchPointFactory(
        lead=lead, kind=TouchPoint.Kind.TP1_WELCOME, status=TouchPoint.Status.SCHEDULED
    )
    assert services.mark_lost(lead, "  Requester canceled ") is lead
    lead.refresh_from_db()
    assert lead.status == Lead.Status.LOST and lead.lost_reason == "Requester canceled"
    review.refresh_from_db()
    welcome.refresh_from_db()
    assert review.status == welcome.status == TouchPoint.Status.CANCELLED

    other = LeadFactory(status=Lead.Status.NEW)
    services.mark_lost(other, " ")
    other.refresh_from_db()
    assert other.lost_reason == "Marked lost"


def test_mark_lost_refuses_booked():
    booked = LeadFactory(status=Lead.Status.BOOKED)
    with pytest.raises(services.LeadStateError, match="Orders console"):
        services.mark_lost(booked, "x")
    booked.refresh_from_db()
    assert booked.status == Lead.Status.BOOKED

    lost = LeadFactory(status=Lead.Status.LOST, lost_reason="Too far")
    with pytest.raises(services.LeadStateError, match="Already marked lost"):
        services.mark_lost(lost, "x")
    lost.refresh_from_db()
    assert lost.lost_reason == "Too far"
