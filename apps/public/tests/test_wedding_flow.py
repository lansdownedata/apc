"""The wedding intake end to end: the page, the single POST, the thanks page, resume."""

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.leads.models import Lead

from .test_wedding_form import _legs, _post

pytestmark = pytest.mark.django_db

PLAN_URL = "/weddings/plan/"


@pytest.fixture(autouse=True)
def _clear_throttle():
    cache.clear()


# --- the page ----------------------------------------------------------------------


def test_the_planner_is_public(client):
    assert client.get(PLAN_URL).status_code == 200


def test_the_page_boots_the_alpine_planner(client):
    html = client.get(PLAN_URL).content.decode()
    assert "weddingPlanner(" in html
    assert "/weddings/venues/" in html


def test_the_page_never_offers_a_vehicle_question(client):
    """Headcount plus trip shape produces the recommendation — spec §5.1 step 3."""
    html = client.get(PLAN_URL).content.decode().lower()
    assert "what kind of vehicle" not in html


def test_the_recommendation_note_no_longer_pitches_a_looping_shuttle(client):
    """Client feedback A3(3): never nudge a couple toward one coach looping for guests."""
    html = client.get(PLAN_URL).content.decode()
    assert "confirm the best fit" in html
    assert "smaller coach running" not in html
    assert "works out cheaper" not in html
    assert "vehicle type?" not in html


def test_the_page_carries_the_honeypot(client):
    assert 'name="company"' in client.get(PLAN_URL).content.decode()


def test_the_page_uses_no_native_select_or_dialog(client):
    """CLAUDE.md: never a BARE <select> for an option input.

    This used to assert the page had no <select> at all, which held only while it had
    none of any kind. The booking panel in the shell now ships one, and it is the
    sanctioned form — `data-tom`, enhanced by initTomSelects(). So the guard checks
    what the rule actually says: every select on the page is a Tom Select.
    """
    html = client.get(PLAN_URL).content.decode()
    for fragment in html.split("<select")[1:]:
        assert "data-tom" in fragment.split(">")[0], "bare <select> on the wedding page"
    assert "window.confirm" not in html and "window.alert" not in html


# --- the single POST ---------------------------------------------------------------


def test_a_submission_creates_one_lead_and_redirects_to_thanks(client):
    resp = client.post(PLAN_URL, _post())
    assert resp.status_code == 302
    assert "/bookings/thanks/" in resp["Location"]
    lead = Lead.objects.get()
    assert lead.reservations.count() == 2


def test_the_honeypot_blocks_the_wedding_form_too(client):
    resp = client.post(PLAN_URL, _post(company="spam"))
    assert Lead.objects.count() == 0
    assert resp.status_code == 200


def test_an_invalid_submission_re_renders_with_errors(client):
    resp = client.post(PLAN_URL, _post(name=""))
    assert resp.status_code == 200
    assert resp.context["form"].errors
    assert Lead.objects.count() == 0


def test_the_wedding_post_is_throttled_like_the_booking_post(client):
    from apps.public.views import BOOKING_THROTTLE_LIMIT

    for i in range(BOOKING_THROTTLE_LIMIT):
        assert client.post(PLAN_URL, _post(email=f"j{i}@example.com")).status_code == 302
    resp = client.post(PLAN_URL, _post(email="one-too-many@example.com"))
    assert resp.status_code == 200
    assert Lead.objects.count() == BOOKING_THROTTLE_LIMIT


def test_an_invalid_submission_never_spends_the_throttle(client):
    for _ in range(6):
        client.post(PLAN_URL, _post(name=""))
    assert client.post(PLAN_URL, _post()).status_code == 302


# --- the thanks page ---------------------------------------------------------------


def test_the_thanks_page_lists_the_movements_and_the_reference(client):
    resp = client.post(PLAN_URL, _post(), follow=True)
    body = resp.content.decode()
    lead = Lead.objects.get()
    assert lead.quote_no in body
    assert "Hampton Inn Leesburg" in body  # the movement's route, both ends
    assert "The Oak Barn at Loyalty" in body
    assert "3:00 PM" in body
    assert "105 passengers" in body


def test_the_plain_thanks_page_still_works_without_a_token(client):
    assert client.get("/bookings/thanks/").status_code == 200


def test_a_forged_thanks_token_falls_back_to_the_plain_page(client):
    resp = client.get("/bookings/thanks/?w=not-a-real-token")
    assert resp.status_code == 200
    assert "Movements" not in resp.content.decode()


# --- resume (spec §7.4) ------------------------------------------------------------


def _resume_url(lead) -> str:
    from apps.public.services import make_wedding_token

    return f"/weddings/plan/{make_wedding_token(lead)}/"


def test_the_confirmation_email_carries_a_resume_link(client, mailoutbox, settings):
    settings.PUBLIC_BASE_URL = "https://allprocharter.com"
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["jane@example.com"]
    assert _resume_url(lead) in mailoutbox[0].body


def test_no_email_is_attempted_when_only_a_phone_was_given(client, mailoutbox):
    client.post(PLAN_URL, _post(email="", phone="2024242600"))
    assert mailoutbox == []


def test_a_resume_link_rehydrates_the_saved_plan(client):
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    html = client.get(_resume_url(lead)).content.decode()
    assert "The Oak Barn at Loyalty" in html
    assert "Guests to the ceremony" in html
    assert "resume" in html


def test_resuming_rebuilds_the_same_lead_rather_than_making_a_second(client):
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    resp = client.post(_resume_url(lead), _post(legs_json=json.dumps(_legs(3))))
    assert resp.status_code == 302
    assert Lead.objects.count() == 1
    lead.refresh_from_db()
    assert lead.reservations.count() == 3


def test_a_forged_resume_token_is_a_404(client):
    assert client.get("/weddings/plan/forged-token/").status_code == 404


def test_a_resume_token_for_a_deleted_lead_is_a_404(client):
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    url = _resume_url(lead)
    lead.reservations.all().delete()
    lead.delete()
    assert client.get(url).status_code == 404


def test_the_saved_payload_round_trips_every_answer(client):
    client.post(PLAN_URL, _post(groups="guests,party", guest_count="105", hotels_tbd="1"))
    payload = Lead.objects.get().intake_payload
    assert payload["groups"] == ["guests", "party"]
    assert payload["guest_count"] == 105
    assert payload["hotels_tbd"] is True
    assert payload["venue_name"] == "The Oak Barn at Loyalty"


def test_a_lead_that_predates_the_payload_still_resumes(client):
    """An older wedding lead (or one an agent rebuilt) must not 500 the resume link."""
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    Lead.objects.filter(pk=lead.pk).update(intake_payload={})
    resp = client.get(_resume_url(lead))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "The Oak Barn at Loyalty" in body  # legs rebuilt from the reservations
    assert "15:00" in body


# --- alerts surface in the pipeline ------------------------------------------------


def test_a_wedding_inside_the_alert_window_arrives_flagged(client):
    soon = (timezone.localdate() + timedelta(days=20)).isoformat()
    client.post(PLAN_URL, _post(wedding_date=soon))
    assert Lead.objects.get().has_alert is True


def test_the_customer_never_sees_how_a_leg_bills(client):
    """Transfer-vs-hourly and the hours override are office controls. No pricing
    mechanics reach the public flow — it shows vehicle recommendations and nothing else."""
    html = client.get(PLAN_URL).content.decode()
    assert "setLegTripType" not in html
    assert "trip_types_json" not in html
    assert "Bills as" not in html
    assert "leg-hours-" not in html
