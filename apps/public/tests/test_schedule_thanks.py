"""The page Calendly redirects an invitee to after they book.

Every value on it arrives in the query string, which anyone can forge — so these
tests pin display-only behaviour and escaping, never persistence.
"""

from django.test import Client

URL = "/schedule/thanks/"


def test_renders_without_any_params(db):
    resp = Client().get(URL)
    assert resp.status_code == 200
    assert b"on the calendar" in resp.content


def test_shows_invitee_name_and_local_time(db):
    resp = Client().get(
        URL, {"invitee_full_name": "Sarah Smith", "event_start_time": "2026-09-08T18:30:00Z"}
    )
    html = resp.content.decode()
    assert "Sarah Smith" in html
    # 18:30 UTC on 2026-09-08 is 2:30 PM in America/New_York, and the abbreviation
    # has to show or a visitor in another timezone is guessing.
    assert "2:30 PM" in html
    assert "EDT" in html


def test_html_in_params_is_escaped(db):
    html = Client().get(URL, {"invitee_full_name": "<script>alert(1)</script>"}).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_absurdly_long_name_is_truncated(db):
    """Calendly truncates nothing; a hand-built URL must not stretch the layout."""
    html = Client().get(URL, {"invitee_full_name": "x" * 500}).content.decode()
    assert "x" * 500 not in html


def test_unparseable_start_time_degrades_quietly(db):
    resp = Client().get(URL, {"event_start_time": "not-a-date"})
    assert resp.status_code == 200
    assert b"on the calendar" in resp.content


def test_naive_start_time_is_rejected(db):
    """No offset means we'd be guessing the zone — better to show nothing."""
    html = Client().get(URL, {"event_start_time": "2026-09-08T18:30:00"}).content.decode()
    assert "EDT" not in html


def test_offers_the_next_step(db):
    html = Client().get(URL).content.decode()
    assert 'href="/bookings/"' in html


def test_is_noindex(db):
    """A destination, not something anyone should reach from search."""
    html = Client().get(URL).content.decode()
    assert 'name="robots" content="noindex"' in html
