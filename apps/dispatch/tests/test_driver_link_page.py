"""Driver trip page: the server-rendered markup the driver sees before any script runs.

The page script (static/js/driver_page.js) keeps these parts in step with each POST; the
first paint is the server's, so a driver with a slow phone still sees the right button.
"""

import html
import re
from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.dispatch.driver_link_views import page_state
from apps.dispatch.models import DispatchAlertConfig
from apps.dispatch.tests.test_driver_link_views import _live, _place_stops, _url
from apps.reservations.acknowledgements import make_driver_link_token
from apps.reservations.models import Passenger, Reservation

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
HIDDEN = 'style="display: none"'


def _part(page: str, name: str) -> str | None:
    """The opening tag of the element marked `data-part="<name>"`, or None."""
    match = re.search(rf'<[a-z]+\b[^>]*data-part="{name}"[^>]*>', page)
    return match.group(0) if match else None


def _part_text(page: str, name: str) -> str:
    """The unescaped text inside the element marked `data-part="<name>"`."""
    match = re.search(rf'<([a-z]+)\b[^>]*data-part="{name}"[^>]*>(.*?)</\1>', page, re.S)
    assert match, f"no data-part={name!r}"
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()


def _disabled(tag: str) -> bool:
    """The tag carries a bare `disabled` attribute (not `:disabled=` or `disabled:` classes)."""
    return re.search(r"\sdisabled(?=[\s>])", tag) is not None


def _page(client, assignment, **kw) -> str:
    return client.get(_url(assignment), **kw).content.decode()


@pytest.mark.parametrize(
    ("status", "label"),
    [
        (S.DISPATCHED, "I'm on the way"),
        (S.ON_THE_WAY, "I've arrived"),
        (S.ARRIVED, "Passenger on board"),
        (S.CUSTOMER_IN_CAR, "Trip complete"),
    ],
)
def test_big_button_label_per_status(client, status, label):
    page = _page(client, _live(status=status))
    go = _part(page, "go")
    assert go is not None and go.startswith("<button")
    assert not _disabled(go)
    assert HIDDEN not in go
    assert _part_text(page, "go") == label


def test_done_shows_thank_you_and_no_button(client):
    page = _page(client, _live(status=S.DONE))
    assert _part(page, "go") is None
    assert _part(page, "back") is None
    thanks = _part(page, "thanks")
    assert thanks is not None and HIDDEN not in thanks
    assert _part_text(page, "thanks") == "Trip complete · thank you"


def test_before_window_copy_and_disabled_button(client):
    a = _live(pickup_date=timezone.localdate() + timedelta(days=3))
    opens = page_state(a)["opens_at_label"]
    assert opens
    page = _page(client, a)
    assert _disabled(_part(page, "go"))
    assert HIDDEN not in _part(page, "opens")
    assert _part_text(page, "opens") == f"Opens at {opens}, 6 hours before pickup"
    # Everything else stays visible: the stop list and the dispatch line are on the page.
    assert _part(page, "stops") is not None


def test_open_window_hides_the_opens_line(client):
    page = _page(client, _live())
    assert HIDDEN in _part(page, "opens")


def test_call_passenger_hidden_without_contact(client):
    a = _live()
    assert "Call passenger" not in _page(client, a)

    Passenger.objects.create(
        reservation=a.reservation, name="Pat Rider", mobile="+15551230000", is_lead=True
    )
    page = _page(client, a)
    assert 'href="tel:+15551230000"' in page
    assert "Call passenger" in page

    DispatchAlertConfig.objects.all().delete()
    DispatchAlertConfig.objects.create(driver_page_show_passenger_contact=False)
    assert "Call passenger" not in _page(client, a)


def test_maplibre_loaded_only_with_public_key(client):
    a = _live()
    _place_stops(a, (40.0, -74.0), (40.1, -74.1))
    with override_settings(LOCATIONIQ_PUBLIC_KEY=""):
        assert "maplibre" not in _page(client, a).lower()
    with override_settings(LOCATIONIQ_PUBLIC_KEY="pk.public"):
        page = _page(client, a)
    assert "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js" in page
    assert "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css" in page


def test_go_back_link_hidden_at_dispatched(client):
    back = _part(_page(client, _live(status=S.DISPATCHED)), "back")
    assert back is not None and HIDDEN in back
    back = _part(_page(client, _live(status=S.ON_THE_WAY)), "back")
    assert back is not None and HIDDEN not in back
    assert back.startswith("<button")


def test_circling_toggle_only_while_on_the_way_or_arrived(client):
    assert HIDDEN in _part(_page(client, _live(status=S.DISPATCHED)), "circling")
    circling = _part(_page(client, _live(status=S.ARRIVED)), "circling")
    assert HIDDEN not in circling and 'aria-pressed="false"' in circling


def test_waiting_on_dispatch_shows_no_big_button(client):
    page = _page(client, _live(status=S.ASSIGNED))
    assert HIDDEN in _part(page, "go")
    assert _part_text(page, "message") == "Waiting on dispatch"


def test_next_stop_card_follows_the_status(client):
    a = _live(status=S.DISPATCHED)
    pickup, dropoff = a.reservation.ordered_stops
    pickup.name, dropoff.name = "Hay-Adams", "Dulles Intl"
    pickup.save(update_fields=["name"])
    dropoff.save(update_fields=["name"])
    page = _page(client, a)
    assert HIDDEN not in _part(page, "next-pickup")
    assert HIDDEN in _part(page, "next-dropoff")
    assert _part_text(page, "eyebrow") == "Next · Pickup"
    assert _part_text(page, "next-name") == "Hay-Adams"

    page = _page(client, _live(status=S.CUSTOMER_IN_CAR))
    assert HIDDEN in _part(page, "next-pickup")
    assert HIDDEN not in _part(page, "next-dropoff")
    assert "Next · Drop-off" in page


def test_progress_bar_marks_the_steps_reached(client):
    page = _page(client, _live(status=S.ON_THE_WAY))
    bar = re.search(r'data-part="progress"[^>]*>(.*?)</div>', page, re.S).group(1)
    filled = re.findall(r"<span[^>]*bg-gold[^>]*>", bar)
    assert len(filled) == 5
    assert sum(HIDDEN not in tag for tag in filled) == 2


def test_dispatch_tab_calls_and_texts_dispatch(client):
    with override_settings(COMPANY_PHONE="+15550100"):
        page = _page(client, _live())
    assert 'href="tel:+15550100"' in page
    assert 'href="sms:+15550100"' in page
    assert "Report a no-show" in page


def test_page_script_and_data_are_included(client):
    page = _page(client, _live())
    assert re.search(r'src="/static/js/driver_page\.js\?v=[^"]*"', page)
    assert 'x-data="driverPage()"' in page
    assert 'id="driver-data"' in page


@pytest.mark.parametrize("dead", [False, True])
def test_token_never_in_canonical_or_og_url(client, dead):
    a = _live()
    if dead:
        a.status = a.Status.WITHDRAWN
        a.save(update_fields=["status"])
    token = make_driver_link_token(a)
    page = client.get(reverse("driver_link", args=[token])).content.decode()
    tags = re.findall(r'<link[^>]*rel="canonical"[^>]*>', page)
    tags += re.findall(r'<meta[^>]*property="og:url"[^>]*>', page)
    for tag in tags:
        assert token not in tag
    assert '<meta name="robots" content="noindex">' in page


def test_map_resizes_to_its_frame_before_fitting_bounds():
    from pathlib import Path

    js = Path("static/js/driver_page.js").read_text()
    assert "ResizeObserver" in js
    assert js.index("resize()") < js.index("fitBounds(")
