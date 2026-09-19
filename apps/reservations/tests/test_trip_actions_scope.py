"""Duplicate and copy-to-dates work outside the quote workspace's Alpine scope.

`components/trip_line.html` renders the same trip on the quote and on the order page, but
these two actions reached into `quoteWorkspace()` — `duplicateReservation()` was a method
on it and the copy-dates modal read `copyDatesOpen`/`copyDatesFor` off it — so the partial
could only offer them where that scope existed.

They now follow the same route the trip editor took: a global function, and a window event
the modal listens for. Any page listing trips can offer both.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
TRIP_LINE = (ROOT / "templates" / "components" / "trip_line.html").read_text()
MODAL = (ROOT / "templates" / "leads" / "_copy_dates_modal.html").read_text()
ORDER = (ROOT / "templates" / "orders" / "order_detail.html").read_text()


def _workspace() -> str:
    """Just the quoteWorkspace() body."""
    start = APP_JS.index("function quoteWorkspace(")
    return APP_JS[start : APP_JS.index("\nwindow.quoteWorkspace", start)]


# --- duplicate ------------------------------------------------------------------------


def test_duplicate_is_a_global_not_a_workspace_method():
    assert "window.duplicateReservation = duplicateReservation" in APP_JS
    assert "function duplicateReservation(pk)" in APP_JS
    assert "duplicateReservation(pk) {" not in _workspace(), "still a workspace method"


# --- copy to dates --------------------------------------------------------------------


def test_the_copy_dates_modal_carries_its_own_scope():
    assert "function copyDates(" in APP_JS
    assert "window.copyDates = copyDates" in APP_JS
    assert 'x-data="copyDates()"' in MODAL


def test_the_modal_opens_from_a_window_event():
    """The same way the trip editor is opened, so the trip line needs no shared scope."""
    assert "copy-dates-open" in APP_JS
    assert "copy-dates-open" in TRIP_LINE
    assert "openCopyDates" not in APP_JS, "the workspace method is back"
    assert "openCopyDates" not in TRIP_LINE


def test_the_workspace_no_longer_holds_copy_dates_state():
    body = _workspace()
    for member in ("copyDatesOpen", "copyDatesFor", "copyDatesSource"):
        assert member not in body, f"{member} is still workspace state"


# --- the shared partial offers both, everywhere ---------------------------------------


def test_both_controls_sit_outside_the_sales_only_block():
    """Whatever the page asks for, a trip can be copied and recurred; what the sales gate
    keeps is the destructive set."""
    gate = TRIP_LINE.index('{% if actions == "sales" %}')
    head = TRIP_LINE[:gate]
    assert "duplicateReservation(" in head
    assert "copy-dates-open" in head
    tail = TRIP_LINE[gate:]
    for destructive in ("reservation_reverse", "reservation_return", "reservation_delete"):
        assert destructive in tail


def test_the_order_page_includes_the_modal_and_documents_what_it_drops():
    assert '{% include "leads/_copy_dates_modal.html" %}' in ORDER
    assert "duplicate, copy-to-dates, reverse, remove" not in ORDER, "stale comment"


# --- where the endpoints send the agent back ------------------------------------------


def test_both_forms_carry_where_the_agent_was():
    assert 'name="next" value="{{ request.path }}"' in TRIP_LINE
    assert 'name="next" value="{{ request.path }}"' in MODAL
