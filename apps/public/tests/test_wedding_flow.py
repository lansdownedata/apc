"""The wedding intake end to end: the page, the single POST, the thanks page, resume."""

from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.leads.models import Lead

from .test_wedding_form import _post

pytestmark = pytest.mark.django_db

PLAN_URL = "/weddings/plan/"

APP_JS = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()


def _fn_source(name: str) -> str:
    """The body of a top-level `function <name>(` up to its `window.<name> =` export.

    Built with str.split rather than a slice expression: Tailwind's JIT scans this file
    and would turn a bracket-colon slice into a junk arbitrary-value class (CLAUDE.md).
    """
    after = APP_JS.split(f"function {name}(", 1)[1]
    return after.split(f"window.{name} =", 1)[0]


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


def test_every_wedding_step_offers_a_schedule_a_call_affordance(client):
    """APC-10 / A4 — a 'Schedule a call' out is in the footer of every step, not just
    once at the bottom of the page."""
    html = client.get(PLAN_URL).content.decode()
    # one per step footer: date, venue, who, hotels, times, itinerary, contact
    assert html.count("Stuck on something?") >= 6
    # opens the on-page scheduler (or the tel: fallback when CALENDLY_URL is blank)
    assert "openScheduler()" in html or 'href="tel:' in html


def test_wedding_flow_wires_browser_back_to_the_previous_step(client):
    """APC-5 / A2 — every step advance is a history entry, Back walks one step, and the
    on-page Back button shares that route (`history.back()`)."""
    planner = _fn_source("weddingPlanner")
    assert 'addEventListener("popstate"' in planner
    assert "history.pushState(" in planner
    assert "history.replaceState(" in planner
    assert "history.back()" in planner
    # the single code route: one internal mover that popstate replays without pushing
    assert "_goto(" in planner


def test_wedding_flow_persists_answers_across_a_full_navigation(client):
    """APC-5 / A2 — leaving the page and coming back must not wipe entered answers."""
    planner = _fn_source("weddingPlanner")
    assert "sessionStorage" in planner
    assert 'addEventListener("pagehide"' in planner
    # a completed submission clears the saved plan so it doesn't resurrect
    assert planner.count("sessionStorage") >= 3  # persist + restore + clear


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


def test_the_plain_thanks_page_still_works_without_a_token(client):
    assert client.get("/bookings/thanks/").status_code == 200


def test_a_forged_thanks_token_falls_back_to_the_plain_page(client):
    resp = client.get("/bookings/thanks/?w=not-a-real-token")
    body = resp.content.decode()
    assert resp.status_code == 200
    # Assert on the wedding variant's own headline, not on a label it happens to carry:
    # a heading that gets reworded silently turns this into a test of nothing.
    assert "with our dispatch team" not in body
    assert "We will follow up within one business day" in body


# --- resume (spec §7.4) ------------------------------------------------------------


def _resume_url(lead) -> str:
    from apps.public.services import make_wedding_token

    return f"/weddings/plan/{make_wedding_token(lead)}/"


def read_wedding_token(token: str):
    from apps.public.services import read_wedding_token as _read

    return _read(token)


def test_the_confirmation_email_carries_a_resume_link(client, mailoutbox, settings):
    settings.PUBLIC_BASE_URL = "https://allprocharter.com"
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["jane@example.com"]
    # Not the whole signed URL: `signing.dumps` stamps the current time into the token,
    # so a URL rebuilt here differs from the emailed one whenever a second ticks over
    # between the POST and this line — a flake that only shows up under a full run.
    # What matters is that a resume link for THIS lead went out.
    body = mailoutbox[0].body
    assert "https://allprocharter.com/weddings/plan/" in body
    assert read_wedding_token(body.split("/weddings/plan/")[1].split("/")[0]) == lead


def test_no_email_is_attempted_when_only_a_phone_was_given(client, mailoutbox):
    client.post(PLAN_URL, _post(email="", phone="2024242600"))
    assert mailoutbox == []


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


# --- alerts surface in the pipeline ------------------------------------------------


def test_a_wedding_inside_the_alert_window_arrives_flagged(client):
    soon = (timezone.localdate() + timedelta(days=20)).isoformat()
    client.post(PLAN_URL, _post(wedding_date=soon))
    assert Lead.objects.get().has_alert is True


def test_a_fresh_visit_seeds_a_real_null_resume_not_the_string(client):
    """`json_attr` already renders None as `null`; a `default:'null'` in front of it
    JSON-encodes the *word*, and `weddingPlanner`'s `opts.resume` becomes a truthy
    string that every `saved && saved.x` read then silently misses."""
    html = client.get("/weddings/plan/").content.decode()
    assert "resume: null" in html
    assert "resume: &quot;null&quot;" not in html


def test_the_wedding_flow_uses_no_native_date_or_time_inputs(client):
    """The same rule the reservation editor is already held to.

    Chrome's `<input type="time">` renders its own unstyleable picker — bright blue
    spinner columns in the middle of the charcoal/gold flow. The date step was already
    on flatpickr; the times step and the itinerary's per-leg inputs were missed.
    """
    html = client.get(PLAN_URL).content.decode()
    assert 'type="time"' not in html
    assert 'type="date"' not in html
    assert "data-flatpickr-time" in html


def test_the_public_shell_serves_flatpickr_to_the_wedding_page(client):
    html = client.get(PLAN_URL).content.decode()
    assert "flatpickr.min.js" in html and "flatpickr.min.css" in html


def test_resuming_updates_the_same_lead_rather_than_making_a_second(client):
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    resp = client.post(_resume_url(lead), _post(guest_count="140"))
    assert resp.status_code == 302
    assert Lead.objects.count() == 1
    lead.refresh_from_db()
    assert lead.intake_payload["guest_count"] == 140


def test_a_lead_with_no_saved_answers_still_resumes(client):
    """An older wedding lead whose payload is gone must not 500 the emailed link."""
    client.post(PLAN_URL, _post())
    lead = Lead.objects.get()
    Lead.objects.filter(pk=lead.pk).update(intake_payload={})
    assert client.get(_resume_url(lead)).status_code == 200
