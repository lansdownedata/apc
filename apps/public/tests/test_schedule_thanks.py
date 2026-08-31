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


# --- the signed-token path (our own booking form) ---------------------------------
#
# Bookings made through our form redirect here with `?b=<signed token>`, so the page
# shows a real name and time for EVERY booking rather than only the direct-link traffic
# the query-string path was built for.

import pytest  # noqa: E402
from django.core import signing  # noqa: E402

from apps.public.services import make_booking_token  # noqa: E402


def _token(**overrides):
    payload = {
        "name": "Sarah Whitfield",
        "start_time": "2026-09-08T18:30:00Z",
        "timezone": "America/New_York",
    }
    payload.update(overrides)
    return make_booking_token(**payload)


def test_a_token_renders_the_real_name_and_time(db):
    html = Client().get(URL, {"b": _token()}).content.decode()
    assert "Sarah Whitfield" in html
    assert "2:30 PM" in html
    assert "EDT" in html


def test_the_time_renders_in_the_zone_the_visitor_booked_in(db):
    """The one page on the site that does not render in TIME_ZONE.

    The confirmation has to agree with the slot they clicked and with the calendar
    invite Calendly emails them, both of which are in THEIR zone. Showing 2:30 PM EDT
    to someone who booked 11:30 AM PDT reads as a different appointment.
    """
    html = Client().get(URL, {"b": _token(timezone="America/Los_Angeles")}).content.decode()
    assert "11:30 AM" in html
    assert "PDT" in html
    assert "2:30 PM" not in html


def test_a_zone_that_is_not_a_real_zone_falls_back_rather_than_500ing(db):
    """ZoneInfo raises on garbage, and this is an unauthenticated page."""
    html = Client().get(URL, {"b": _token(timezone="Mars/Olympus_Mons")}).content.decode()
    assert "Sarah Whitfield" in html
    assert "2:30 PM" in html
    assert "EDT" in html


def test_a_blank_zone_falls_back_too(db):
    html = Client().get(URL, {"b": _token(timezone="")}).content.decode()
    assert "2:30 PM" in html


def test_a_tampered_token_degrades_to_the_generic_page(db):
    resp = Client().get(URL, {"b": _token() + "x"})
    assert resp.status_code == 200
    assert b"on the calendar" in resp.content
    assert b"Sarah Whitfield" not in resp.content


def test_a_token_signed_with_another_salt_is_ignored(db):
    forged = signing.dumps({"name": "Mallory", "start_time": "", "timezone": ""}, salt="nope")
    html = Client().get(URL, {"b": forged}).content.decode()
    assert "Mallory" not in html


def test_an_expired_token_degrades_rather_than_erroring(db, settings):
    from apps.public import services

    token = _token()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(services, "BOOKING_TOKEN_MAX_AGE_SECONDS", -1)
        resp = Client().get(URL, {"b": token})
    assert resp.status_code == 200
    assert b"Sarah Whitfield" not in resp.content


def test_a_token_with_a_junk_start_time_still_greets_them_by_name(db):
    html = Client().get(URL, {"b": _token(start_time="whenever")}).content.decode()
    assert "Sarah Whitfield" in html
    assert "EDT" not in html


def test_a_name_in_a_token_is_still_escaped(db):
    """Signed does not mean trusted-to-render: we sign whatever the form posted."""
    html = Client().get(URL, {"b": _token(name="<script>alert(1)</script>")}).content.decode()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_token_wins_over_a_forged_query_string(db):
    """Query-string values are display-only and forgeable; the token is the trusted
    path, so a URL carrying both must not let the forgeable half through."""
    html = Client().get(URL, {"b": _token(), "invitee_full_name": "Mallory"}).content.decode()
    assert "Sarah Whitfield" in html
    assert "Mallory" not in html
