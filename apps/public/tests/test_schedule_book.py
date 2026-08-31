"""POST /schedule/book/ — creating the booking on the visitor's behalf.

The endpoint's job ends at "Calendly accepted". It creates no Lead and no Contact:
`invitee.created` already does that, idempotently, with the attach heuristic and
reschedule correlation, and it fires for an API booking exactly as for a UI one
(probed 2026-08-31). Creating one here would race the webhook and duplicate.
"""

from datetime import UTC, timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.contacts.models import Contact
from apps.integrations.calendly import CalendlyAPIError, CalendlySlotTaken
from apps.leads.models import Lead
from apps.public.models import SlotHold
from apps.public.services import read_booking_token

pytestmark = pytest.mark.django_db

URL = "/schedule/book/"

QUESTIONS = [
    {
        "name": "Please share anything that will help.",
        "type": "text",
        "position": 0,
        "required": False,
    },
    {"name": "Event Date", "type": "string", "position": 1, "required": True},
    {"name": "What is your phone number?", "type": "phone_number", "position": 2, "required": True},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _start(days=3):
    return (timezone.now() + timedelta(days=days)).replace(microsecond=0)


def _iso(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _form(**overrides):
    data = {
        "name": "Sarah Whitfield",
        "email": "sarah@example.com",
        "phone": "703-555-0101",
        "timezone": "America/New_York",
        "start_time": _iso(_start()),
        "q1": "2026-11-12",
        "q2": "703-555-0101",
    }
    data.update(overrides)
    return data


def _post(client=None, questions=None, invitee=None, **overrides):
    client = client or Client()
    with patch(
        "apps.public.views.calendly.event_type_questions",
        return_value=QUESTIONS if questions is None else questions,
    ):
        with patch("apps.public.views.calendly.create_invitee", **(invitee or {})) as create:
            resp = client.post(URL, _form(**overrides))
    return resp, create


# --- the happy path -------------------------------------------------------------


def test_a_booking_calls_calendly_once_and_redirects():
    resp, create = _post()
    assert resp.status_code == 200
    assert create.call_count == 1
    assert resp.json()["redirect"].startswith("/schedule/thanks/?b=")


def test_the_redirect_token_carries_the_name_time_and_zone():
    """The thanks page renders from this, so every booking gets a real confirmation —
    not just the direct-link traffic the query-string path was built for."""
    resp, _ = _post()
    token = resp.json()["redirect"].split("?b=", 1)[1]
    payload = read_booking_token(token)
    assert payload["name"] == "Sarah Whitfield"
    assert payload["timezone"] == "America/New_York"
    assert payload["start_time"] == _iso(_start())


def test_the_token_carries_no_email_or_phone():
    """It lands in a URL, and a URL ends up in history, referrers and logs."""
    resp, _ = _post()
    payload = read_booking_token(resp.json()["redirect"].split("?b=", 1)[1])
    assert set(payload) == {"name", "start_time", "timezone"}


def test_the_hold_is_released_once_the_booking_lands():
    """The slot is Calendly's problem from here; leaving our hold up would grey out a
    slot for our own visitors on top of it already being gone."""
    resp, _ = _post()
    assert resp.status_code == 200
    assert SlotHold.objects.claim(_start(), "somebody-else") is not None


def test_no_lead_and_no_contact_are_created_here():
    """Guard against someone helpfully adding it later: the webhook owns lead
    creation, and doing it here too would race it and duplicate."""
    leads, contacts = Lead.objects.count(), Contact.objects.count()
    _post()
    assert Lead.objects.count() == leads
    assert Contact.objects.count() == contacts


# --- the body we send -----------------------------------------------------------


def test_answers_carry_the_position_from_the_live_config():
    """Never hardcoded: `position` is what Calendly matches on and it is the client's
    to reorder in his own account at any time."""
    _, create = _post()
    answers = create.call_args.kwargs["answers"]
    assert {"question": "Event Date", "answer": "2026-11-12", "position": 1} in answers
    assert all("position" in a for a in answers)


def test_a_reordered_question_list_moves_the_answers_with_it():
    """The regression this guards is silent: hardcoded positions keep working right up
    until the client drags a question in Calendly, then post to the wrong field."""
    moved = [
        {"name": "Event Date", "type": "string", "position": 2, "required": True},
        {
            "name": "What is your phone number?",
            "type": "phone_number",
            "position": 1,
            "required": True,
        },
    ]
    _, create = _post(questions=moved, q1="703-555-0101", q2="2026-11-12")
    answers = {a["question"]: a["position"] for a in create.call_args.kwargs["answers"]}
    assert answers["Event Date"] == 2
    assert answers["What is your phone number?"] == 1


def test_the_phone_is_normalised_to_e164_for_the_call_location():
    """It becomes location.location — the number the host actually dials."""
    _, create = _post()
    assert create.call_args.kwargs["phone"] == "+17035550101"


# --- validation, before we spend a call ------------------------------------------


def test_a_blank_phone_is_refused_before_calendly():
    """Phone is location.location, so Calendly's complaint would come back as a
    location failure — an error that names nothing the visitor can act on."""
    resp, create = _post(phone="")
    assert resp.status_code == 400
    assert "phone" in resp.json()["errors"]
    create.assert_not_called()


def test_an_unparseable_phone_is_refused_before_calendly():
    resp, create = _post(phone="not a number")
    assert resp.status_code == 400
    create.assert_not_called()


def test_a_required_custom_question_is_enforced_server_side():
    resp, create = _post(q1="")
    assert resp.status_code == 400
    assert "Event Date" in str(resp.json()["errors"])
    create.assert_not_called()


def test_an_optional_custom_question_may_be_left_blank():
    resp, create = _post(q0="")
    assert resp.status_code == 200
    assert create.call_count == 1


def test_a_blank_optional_answer_is_not_submitted_at_all():
    _, create = _post(q0="")
    assert all(a["answer"] for a in create.call_args.kwargs["answers"])


def test_a_missing_name_or_email_is_refused():
    assert _post(name="")[0].status_code == 400
    assert _post(email="")[0].status_code == 400
    assert _post(email="not-an-email")[0].status_code == 400


def test_a_start_time_that_is_not_a_timestamp_is_refused():
    resp, create = _post(start_time="tomorrow-ish")
    assert resp.status_code == 400
    create.assert_not_called()


def test_a_start_time_in_the_past_is_refused():
    resp, create = _post(start_time=_iso(timezone.now() - timedelta(hours=1)))
    assert resp.status_code == 400
    create.assert_not_called()


# --- races and repeats ------------------------------------------------------------


def test_a_slot_another_visitor_holds_is_refused_without_calling_calendly():
    """Saves the upstream call on a race we can already see locally."""
    SlotHold.objects.claim(_start(), "another-session")
    resp, create = _post()
    assert resp.status_code == 409
    assert resp.json()["code"] == "slot_taken"
    create.assert_not_called()


def test_calendly_saying_already_filled_is_a_409_with_fresh_slots():
    """`already_filled` is the authoritative race signal — a local hold cannot see a
    booking made on calendly.com. The UI re-renders from the slots in this body."""
    with patch("apps.public.views.calendly.available_times", return_value=[]):
        resp, create = _post(invitee={"side_effect": CalendlySlotTaken("gone")})
    assert resp.status_code == 409
    assert resp.json()["code"] == "slot_taken"
    assert "slots" in resp.json()
    assert create.call_count == 1


def test_a_lost_race_releases_the_hold_and_drops_the_stale_cache():
    """The cached grid provably contains a slot that is gone, so serving it again
    would send the next visitor straight back into the same collision."""
    fresh = [{"status": "available", "start_time": _iso(_start(5))}]
    with patch("apps.public.views.calendly.available_times", return_value=fresh) as times:
        resp, _ = _post(invitee={"side_effect": CalendlySlotTaken("gone")})
    assert times.called
    assert resp.json()["slots"]
    assert SlotHold.objects.claim(_start(), "somebody-else") is not None


def test_a_double_click_produces_one_booking_not_two():
    """claim() lets a session re-take its own hold, so nothing upstream of here stops
    the second submit — the session has to remember it already booked."""
    client = Client()
    first, create_a = _post(client=client)
    second, create_b = _post(client=client)
    assert first.status_code == second.status_code == 200
    assert create_a.call_count == 1
    assert create_b.call_count == 0
    assert first.json()["redirect"] == second.json()["redirect"]


def test_a_different_visitor_is_not_short_circuited_by_someone_elses_booking():
    _post(client=Client())
    SlotHold.objects.all().delete()
    _, create = _post(client=Client())
    assert create.call_count == 1


# --- upstream trouble --------------------------------------------------------------


def test_an_upstream_error_is_502_and_releases_the_hold():
    """A leaked hold would grey the slot out for everyone for the full hold window
    over a failure that had nothing to do with availability."""
    resp, _ = _post(invitee={"side_effect": CalendlyAPIError("boom")})
    assert resp.status_code == 502
    assert resp.json()["error"]
    assert SlotHold.objects.claim(_start(), "somebody-else") is not None


def test_a_get_is_not_allowed():
    assert Client().get(URL).status_code == 405


# --- SMS consent ------------------------------------------------------------------
#
# The event type keeps Calendly's SMS-reminder prompt switched on; we fill it from the
# one phone field we already validate rather than asking for the number twice. But the
# number is only ever sent when the visitor has explicitly opted in.


def test_the_reminder_number_is_sent_only_when_they_opt_in():
    _, create = _post(sms_consent="1")
    assert create.call_args.kwargs["text_reminder_number"] == "+17035550101"


def test_an_unticked_box_sends_no_reminder_number():
    """The default. A booking still succeeds and they still get the call — that is the
    service they asked for; only the texting needs opting into."""
    resp, create = _post()
    assert resp.status_code == 200
    assert create.call_args.kwargs["text_reminder_number"] == ""


def test_consent_is_recorded_with_the_wording_they_saw():
    from apps.public.models import SMS_CONSENT_TEXT, BookingConsent

    _post(sms_consent="1")
    row = BookingConsent.objects.latest("created_at")
    assert row.phone == "+17035550101"
    assert row.email == "sarah@example.com"
    assert row.consent_text == SMS_CONSENT_TEXT
    assert row.start_time == _start()


def test_nothing_is_recorded_when_they_do_not_opt_in():
    from apps.public.models import BookingConsent

    before = BookingConsent.objects.count()
    _post()
    assert BookingConsent.objects.count() == before


def test_consent_is_not_recorded_when_the_booking_fails():
    """A record of consent for a meeting that never existed is noise in the one place
    that has to be trustworthy."""
    from apps.integrations.calendly import CalendlyAPIError
    from apps.public.models import BookingConsent

    before = BookingConsent.objects.count()
    _post(sms_consent="1", invitee={"side_effect": CalendlyAPIError("boom")})
    assert BookingConsent.objects.count() == before


def test_a_double_click_records_consent_once():
    from apps.public.models import BookingConsent

    client = Client()
    _post(client=client, sms_consent="1")
    _post(client=client, sms_consent="1")
    assert BookingConsent.objects.count() == 1
