"""Lead status state machine: the rule table + view guards."""

import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory
from apps.leads.models import ALLOWED_TRANSITIONS, Lead

pytestmark = pytest.mark.django_db

JSON_HEADERS = {"HTTP_ACCEPT": "application/json"}


def test_transition_table_is_exactly_the_spec():
    S = Lead.Status
    assert ALLOWED_TRANSITIONS == {
        S.NEW: {S.QUOTED, S.LOST},
        S.QUOTED: {S.LOST, S.BOOKED},
        S.LOST: {S.NEW},
        S.BOOKED: set(),
    }


def test_can_transition():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    assert not lead.can_transition(Lead.Status.LOST)
    assert LeadFactory(status=Lead.Status.NEW).can_transition(Lead.Status.LOST)


def test_mark_lost_refuses_booked(logged_in_client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    resp = logged_in_client.post(
        reverse("lead_mark_lost", args=[lead.pk]), {"reason": "x"}, **JSON_HEADERS
    )
    assert resp.status_code == 400
    assert "Orders console" in resp.json()["error"]
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_mark_lost_refuses_already_lost(logged_in_client):
    lead = LeadFactory(status=Lead.Status.LOST)
    resp = logged_in_client.post(
        reverse("lead_mark_lost", args=[lead.pk]), {"reason": "x"}, **JSON_HEADERS
    )
    assert resp.status_code == 400


def test_mark_lost_still_works_for_new_and_quoted(logged_in_client):
    for status in (Lead.Status.NEW, Lead.Status.QUOTED):
        lead = LeadFactory(status=status)
        resp = logged_in_client.post(
            reverse("lead_mark_lost", args=[lead.pk]), {"reason": "gone"}, **JSON_HEADERS
        )
        assert resp.json()["ok"] is True
        lead.refresh_from_db()
        assert lead.status == Lead.Status.LOST


def test_reopen_refuses_non_lost(logged_in_client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    resp = logged_in_client.post(reverse("lead_reopen", args=[lead.pk]), **JSON_HEADERS)
    assert resp.status_code == 400
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_non_json_mark_lost_on_booked_redirects_without_change(logged_in_client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    resp = logged_in_client.post(reverse("lead_mark_lost", args=[lead.pk]), {"reason": "x"})
    assert resp.status_code in (302, 400)
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED
