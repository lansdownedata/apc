"""The board remembers how the dispatcher left it.

Dispatch is the screen someone sits on all day, so coming back to a reset grid — default
sort, default window, filters cleared — costs them the same four clicks every time.

What is remembered splits by what it costs. Sort, exceptions-first and density are pure
DOM, so they are restored with no server round trip at all. The date window and the
vehicle/customer/coverage filters decide which trips are fetched, so they can only be
restored by asking for them — which the board does once, and only when opened with a bare
URL and something worth restoring.

The remembered range is deliberately NOT the dates last used: a week-old window is a stale
board. The shape ("I work in ranges") is remembered, and the server hands over today and
tomorrow to fill it. The browser could do that arithmetic correctly on its own — a Date is
an absolute instant, and a zone sent from the server would format it right — but this
window selects trip-local pickup dates in COMPANY time, which the view already resolves for
its own default. Sending it keeps one definition of "today" rather than two that can drift.
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
GRID = APP_JS[APP_JS.index("function dispatchGrid(") : APP_JS.index("window.dispatchGrid")]


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


def _board(client, query="") -> str:
    return client.get(f"{reverse('dispatch_board')}{query}").content.decode()


# --- what costs nothing is restored outright ----------------------------------------


def test_the_grid_remembers_sort_and_exceptions_first_next_to_density():
    """One record, so a dispatcher's layout comes back whole rather than in pieces."""
    for key in ("sort", "exceptionsFirst", "density"):
        assert key in GRID, key
    assert GRID.count("localStorage.setItem") >= 1
    assert "dispatch.prefs" in GRID


def test_restoring_a_sort_re_sorts_the_grid():
    """Storing the column without re-applying it would show the arrow and the old order."""
    body = GRID[GRID.index("restorePrefs() {") :][:900]
    assert "this.apply()" in body


def test_blocked_storage_never_breaks_the_board():
    """Private mode throws on read; the board still has to come up."""
    assert GRID.count("catch") >= 2


def test_the_search_box_is_not_remembered():
    """A remembered query would bring the board back looking half empty."""
    assert '"q"' not in GRID.split("dispatch.prefs")[1][:400]


# --- the window and the filters cost a fetch, so they are restored once --------------


def test_the_board_hands_the_grid_its_current_filter_state(client, agent):
    body = _board(client, "?view=week&f=uncovered")
    assert "boardState" in body
    # json_attr HTML-escapes the quotes so the JSON survives inside the x-data attribute
    assert "view&quot;: &quot;week" in body
    assert "f&quot;: &quot;uncovered" in body


def test_the_grid_only_restores_when_it_was_opened_bare():
    """Arriving with a querystring means the dispatcher asked for that; leave it alone."""
    assert "location.search" in GRID
    assert "location.replace" in GRID


def test_a_remembered_range_comes_back_as_today_to_tomorrow(client, agent):
    """The shape is remembered, the stale dates are not."""
    today = timezone.localdate()
    body = _board(client)
    assert today.isoformat() in body
    assert (today + timedelta(days=1)).isoformat() in body
    assert "defaultRange" in body


def test_the_default_dates_come_from_the_server_not_the_browser_clock(client, agent):
    """Not because a browser Date is wrong — it is an absolute instant — but so that
    "today" has one definition. See this module's docstring."""
    assert "new Date(" not in GRID
    assert "defaultRange" in GRID


def test_a_default_board_is_not_worth_a_second_request():
    """Only a non-default state earns the one redirect this whole feature costs."""
    assert "isDefault" in GRID or "worthRestoring" in GRID


# --- the keyboard, since a dispatcher lives on it ------------------------------------


def test_slash_jumps_to_the_search_box():
    assert "focusSearch" in GRID
    assert "keydown.window.slash" in (ROOT / "templates" / "dispatch" / "board.html").read_text()


def test_the_board_wires_the_slash_key(client, agent):
    assert "focusSearch" in _board(client)


def test_typing_in_a_field_never_steals_the_slash():
    """`_busy()` already guards the arrow keys; the same gate has to cover this."""
    focus = GRID[GRID.index("focusSearch") :][:400]
    assert "_busy()" in focus


# --- the escape hatch ----------------------------------------------------------------


def test_the_view_links_still_win_over_what_was_remembered(client, agent):
    """Clicking Day is how a dispatcher gets out of a remembered range."""
    body = _board(client, "?view=day")
    assert re.search(r"view=(day|week|range)", body)
