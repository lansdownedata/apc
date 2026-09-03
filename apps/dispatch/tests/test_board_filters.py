"""APC-24 — the dispatch board's filter state (view range + vehicle / customer / group)."""

from datetime import date

import pytest
from django.test import RequestFactory
from django.utils import timezone

from apps.dispatch.board_filters import BoardFilters

pytestmark = pytest.mark.django_db


def _filters(**params) -> BoardFilters:
    return BoardFilters.from_request(RequestFactory().get("/dispatch/", params))


def test_default_is_a_single_day_view_of_today():
    f = _filters()

    today = timezone.localdate()
    assert f.view == "day"
    assert f.start == today == f.end


def test_day_view_honours_the_day_param():
    f = _filters(day="2026-09-15")

    assert f.view == "day"
    assert f.start == date(2026, 9, 15) == f.end


def test_a_bad_day_param_falls_back_to_today():
    f = _filters(day="not-a-date")

    assert f.start == timezone.localdate()


def test_week_view_spans_the_monday_to_sunday_around_the_anchor():
    f = _filters(view="week", day="2026-09-16")  # a Wednesday

    assert f.view == "week"
    assert f.start == date(2026, 9, 14)  # Monday
    assert f.end == date(2026, 9, 20)  # Sunday
    assert f.anchor == date(2026, 9, 14)


def test_range_view_takes_explicit_start_and_end():
    f = _filters(view="range", start="2026-09-10", end="2026-09-20")

    assert f.view == "range"
    assert f.start == date(2026, 9, 10)
    assert f.end == date(2026, 9, 20)
    assert f.is_multi_day


def test_range_view_swaps_a_backwards_pair():
    f = _filters(view="range", start="2026-09-20", end="2026-09-10")

    assert f.start == date(2026, 9, 10)
    assert f.end == date(2026, 9, 20)


def test_range_view_is_capped_so_the_query_cannot_run_away():
    f = _filters(view="range", start="2026-01-01", end="2027-01-01")

    assert (f.end - f.start).days <= BoardFilters.MAX_SPAN_DAYS


def test_an_unknown_view_falls_back_to_day():
    f = _filters(view="year")

    assert f.view == "day"


def test_vehicle_customer_and_group_filters_are_parsed():
    f = _filters(
        vehicle="7",
        customer="42",
        group="1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    )

    assert f.vehicle_type_id == 7
    assert f.contact_id == 42
    assert f.group_key == "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"


def test_a_malformed_group_key_is_dropped_not_passed_to_the_query():
    f = _filters(group="not-a-uuid")

    assert f.group_key is None


def test_coverage_filter_only_accepts_the_known_buckets():
    assert _filters(f="uncovered").coverage == "uncovered"
    assert _filters(f="bogus").coverage == ""


def test_is_multi_day_is_false_for_a_single_day():
    assert not _filters().is_multi_day
    assert not _filters(view="range", start="2026-09-10", end="2026-09-10").is_multi_day


def test_nav_steps_by_one_day_in_day_view():
    f = _filters(day="2026-09-15")

    assert f.prev_params()["day"] == "2026-09-14"
    assert f.next_params()["day"] == "2026-09-16"


def test_nav_steps_by_a_week_in_week_view():
    f = _filters(view="week", day="2026-09-16")

    assert f.prev_params()["day"] == "2026-09-07"  # previous Monday
    assert f.next_params()["day"] == "2026-09-21"  # next Monday


def test_nav_shifts_the_whole_window_in_range_view():
    f = _filters(view="range", start="2026-09-10", end="2026-09-14")  # 5 days

    assert f.next_params()["start"] == "2026-09-15"
    assert f.next_params()["end"] == "2026-09-19"
    assert f.prev_params()["start"] == "2026-09-05"


def test_active_filters_survive_the_nav_links():
    f = _filters(day="2026-09-15", vehicle="7", customer="42", f="uncovered")

    nxt = f.next_params()
    assert nxt["vehicle"] == "7"
    assert nxt["customer"] == "42"
    assert nxt["f"] == "uncovered"
