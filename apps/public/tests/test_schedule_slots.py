"""GET /schedule/slots/ — the slot grid's data source.

A thin, cached proxy over Calendly's `event_type_available_times`. It deliberately does
NOT compute availability from schedules and busy times: that means owning buffers,
minimum notice, date overrides and DST, every one a way to offer a slot Calendly then
rejects at booking time.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.integrations.calendly import CalendlyAPIError, CalendlyNotConfigured
from apps.public.models import SlotHold

pytestmark = pytest.mark.django_db

URL = "/schedule/slots/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _slots(*offsets_days):
    """Calendly-shaped slot payloads a fixed number of days out."""
    base = timezone.now() + timedelta(days=1)
    return [
        {
            "status": "available",
            "start_time": (base + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:00.000000Z"),
            "invitees_remaining": 1,
        }
        for d in offsets_days
    ]


def _get(days=7, **patches):
    """Defaults to a single 7-day window — one upstream call, so a `return_value` mock
    is not silently served twice. Paging is exercised explicitly where it matters."""
    params = f"?days={days}" if days is not None else ""
    return Client().get(f"{URL}{params}", **patches)


def test_slots_come_back_as_utc_iso_never_formatted_local_times():
    """The visitor is not necessarily in Eastern. The server has no idea what zone to
    render in, so it renders none — the browser localises from UTC."""
    with patch("apps.public.views.calendly.available_times", return_value=_slots(0)):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    assert resp.status_code == 200
    start = resp.json()["slots"][0]["start"]
    assert start.endswith("Z")
    # No abbreviation, no offset, nothing that implies a wall-clock reading.
    assert "EDT" not in start and "EST" not in start and "+" not in start


def test_a_fortnight_is_paged_across_the_seven_day_cap():
    """Calendly refuses a range wider than 7 days, so a 14-day view is two calls that
    get concatenated. Getting this wrong silently returns half a grid."""
    with patch("apps.public.views.calendly.available_times") as times:
        times.side_effect = [_slots(0, 1), _slots(8, 9)]
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get(14)
    assert times.call_count == 2
    assert len(resp.json()["slots"]) == 4
    # Contiguous, not overlapping: the second window starts where the first ended.
    first, second = times.call_args_list
    assert first.kwargs["end"] == second.kwargs["start"]


def test_two_requests_inside_the_window_make_one_upstream_call():
    """Rate limits are real and tighter than documented."""
    with patch("apps.public.views.calendly.available_times", return_value=_slots(0)) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            _get()
            _get()
    assert times.call_count == 1


def test_a_different_day_range_is_cached_separately():
    with patch("apps.public.views.calendly.available_times", return_value=_slots(0)) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            _get(7)
            _get(14)
    assert times.call_count > 1


def test_a_held_slot_is_flagged_not_dropped():
    """Greying a slot out is honest; silently removing it reshuffles the grid under
    the visitor's cursor between one poll and the next."""
    payload = _slots(0)
    held_at = timezone.datetime.fromisoformat(payload[0]["start_time"].replace("Z", "+00:00"))
    SlotHold.objects.claim(held_at, "someone-else")
    with patch("apps.public.views.calendly.available_times", return_value=payload):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    slots = resp.json()["slots"]
    assert len(slots) == 1
    assert slots[0]["held"] is True


def test_holds_are_applied_after_the_cache_not_baked_into_it():
    """A hold taken between two polls has to show on the second one, even though the
    slot list itself is served from cache."""
    payload = _slots(0)
    held_at = timezone.datetime.fromisoformat(payload[0]["start_time"].replace("Z", "+00:00"))
    with patch("apps.public.views.calendly.available_times", return_value=payload) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            assert _get().json()["slots"][0]["held"] is False
            SlotHold.objects.claim(held_at, "someone-else")
            assert _get().json()["slots"][0]["held"] is True
    assert times.call_count == 1


def test_an_expired_hold_does_not_grey_a_slot():
    payload = _slots(0)
    held_at = timezone.datetime.fromisoformat(payload[0]["start_time"].replace("Z", "+00:00"))
    hold = SlotHold.objects.claim(held_at, "someone-else")
    hold.expires_at = timezone.now() - timedelta(seconds=1)
    hold.save(update_fields=["expires_at"])
    with patch("apps.public.views.calendly.available_times", return_value=payload):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    assert resp.json()["slots"][0]["held"] is False


def test_the_live_question_list_rides_along():
    """The form renders from this, so it must never be hardcoded client-side either."""
    questions = [{"name": "Event Date", "type": "string", "position": 1, "required": True}]
    with patch("apps.public.views.calendly.available_times", return_value=[]):
        with patch("apps.public.views.calendly.event_type_questions", return_value=questions):
            resp = _get()
    assert resp.json()["questions"] == questions


def test_an_upstream_failure_is_503_with_json_never_a_500():
    """The UI falls back to the Calendly popup on this, so it has to be a clean,
    parseable answer rather than a debug page."""
    with patch("apps.public.views.calendly.available_times", side_effect=CalendlyAPIError("nope")):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    assert resp.status_code == 503
    assert resp.json()["error"]
    assert resp.json()["slots"] == []


def test_a_missing_token_degrades_the_same_way():
    with patch(
        "apps.public.views.calendly.available_times", side_effect=CalendlyNotConfigured("no token")
    ):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    assert resp.status_code == 503


def test_days_is_clamped_so_a_hand_built_url_cannot_fan_out():
    """?days=3650 would otherwise be 520 upstream calls from one unauthenticated GET.

    The bound is SLOT_DAYS_MAX at Calendly's 7-day cap — nine calls — not a number
    picked here; what matters is that it is bounded at all.
    """
    from apps.public.views import SLOT_DAYS_MAX

    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get(3650)
    assert resp.status_code == 200
    assert times.call_count <= -(-SLOT_DAYS_MAX // 7)


def test_garbage_days_falls_back_to_the_default_rather_than_erroring():
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            assert Client().get(f"{URL}?days=lots").status_code == 200
    assert times.call_count >= 1


def test_a_slot_calendly_does_not_call_available_is_dropped():
    payload = _slots(0) + [{"status": "unavailable", "start_time": "2026-09-09T12:00:00.000000Z"}]
    with patch("apps.public.views.calendly.available_times", return_value=payload):
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = _get()
    assert len(resp.json()["slots"]) == 1


def test_the_window_starts_in_the_future():
    """Calendly rejects a start_time in the past, and `now` is already past by the time
    the request lands."""
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            _get()
    assert times.call_args_list[0].kwargs["start"] > timezone.now()


# --- month view -------------------------------------------------------------------
#
# The grid is a calendar now: pick a date, then a time. That means fetching a month at
# a go — five upstream calls at Calendly's 7-day cap — so it is cached per month and
# bounded hard against a hand-built ?month=.


def test_a_month_is_fetched_and_echoed_back():
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = Client().get(f"{URL}?month=2026-10")
    assert resp.status_code == 200
    assert resp.json()["month"] == "2026-10"
    assert times.called
    # October is 31 days plus a day of padding either side, at 7 days a call.
    assert times.call_count == 5


def test_the_window_is_padded_past_the_month_edges():
    """A slot at 8pm Eastern on the 30th is the 1st in UTC, and a visitor in Los
    Angeles sees the 1st's early-UTC slots as the 30th. Fetching the bare month would
    drop a day off one end or the other depending on who is looking."""
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            Client().get(f"{URL}?month=2026-10")
    first_start = times.call_args_list[0].kwargs["start"]
    last_end = times.call_args_list[-1].kwargs["end"]
    assert first_start < datetime(2026, 10, 1, tzinfo=UTC)
    assert last_end > datetime(2026, 10, 31, tzinfo=UTC)


def test_the_current_month_starts_from_now_not_the_first():
    """Calendly rejects a start_time in the past, so the 1st is not a legal window
    start once the month is under way."""
    today = timezone.now()
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            Client().get(f"{URL}?month={today:%Y-%m}")
    assert times.call_args_list[0].kwargs["start"] > timezone.now()


def test_a_month_entirely_in_the_past_costs_no_upstream_call():
    with patch("apps.public.views.calendly.available_times") as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = Client().get(f"{URL}?month=2020-01")
    assert resp.status_code == 200
    assert resp.json()["slots"] == []
    times.assert_not_called()


def test_a_month_beyond_the_horizon_costs_no_upstream_call():
    """?month=2999-01 must not fan out, and must not look like an error either — the
    calendar simply has nothing that far out."""
    with patch("apps.public.views.calendly.available_times") as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = Client().get(f"{URL}?month=2999-01")
    assert resp.status_code == 200
    assert resp.json()["slots"] == []
    times.assert_not_called()


def test_a_malformed_month_falls_back_to_the_default_window():
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            resp = Client().get(f"{URL}?month=octoberish")
    assert resp.status_code == 200
    assert times.called


def test_two_requests_for_one_month_make_one_set_of_calls():
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            Client().get(f"{URL}?month=2026-10")
            first = times.call_count
            Client().get(f"{URL}?month=2026-10")
    assert times.call_count == first


def test_different_months_are_cached_separately():
    with patch("apps.public.views.calendly.available_times", return_value=[]) as times:
        with patch("apps.public.views.calendly.event_type_questions", return_value=[]):
            Client().get(f"{URL}?month=2026-10")
            first = times.call_count
            Client().get(f"{URL}?month=2026-11")
    assert times.call_count > first
