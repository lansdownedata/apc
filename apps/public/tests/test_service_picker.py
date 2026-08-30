"""The hero's four-option service picker (spec 2026-08-30 §4).

The site's flagship conversion surface: a visitor picks what the occasion is, and only
then sees a form shaped like it.
"""

import json
import re
from html import unescape

import pytest
from django.test import Client

from apps.leads.models import ServiceType

pytestmark = pytest.mark.django_db


def _home() -> str:
    return Client().get("/").content.decode()


def test_the_hero_offers_all_four_services():
    html = _home()
    for label in ["Wedding", "Airport Transfer", "Corporate", "Something else"]:
        assert label in html


def test_every_card_is_a_real_link():
    """No-JS is not a broken page — each card navigates on its own."""
    html = _home()
    for href in [
        "/weddings/plan/",
        "/bookings/?service=airport",
        "/bookings/?service=corporate",
        "/contact/",
    ]:
        assert f'href="{href}"' in html


def test_wedding_leads_the_grid_as_the_most_requested_option():
    html = _home()
    assert "Most requested" in html
    assert "Shuttles, wedding party, the whole day" in html


def test_the_supporting_copy_is_the_agreed_wording():
    html = _home()
    assert "IAD, DCA, BWI — arrivals and departures" in html
    assert "Roadshows, shuttles, executive travel" in html
    assert "Tell us what you need" in html


def test_the_grid_is_a_labelled_nav():
    assert 'aria-label="What do you need transportation for?"' in _home()


def test_the_standfirst_hands_over_to_the_picker():
    assert "Tell us what the occasion is and we&#x27;ll take it from there." in _home() or (
        "Tell us what the occasion is and we'll take it from there." in _home()
    )


def test_the_existing_booking_widget_is_still_there_for_the_in_place_swap():
    """Airport and Corporate reuse the widget unchanged — never a fork of it."""
    html = _home()
    assert "quoteSteps(" in html
    assert "twoStep: true" in html
    assert "servicePicker(" in html


def test_the_picker_knows_which_service_each_card_preselects():
    """The slug -> ServiceType map reaches the Alpine attribute as valid, escaped JSON.

    Escaping is the point: a raw `"` inside a double-quoted x-data closes the attribute
    and silently kills the whole component in the browser.
    """
    html = _home()
    raw = re.search(r"servicePicker\(\{ services: (.*?) \}\)", html).group(1)
    services = json.loads(unescape(raw))
    assert services["airport"] == ServiceType.objects.get(name="Airport Transfer").pk
    assert services["corporate"] == ServiceType.objects.get(name="Corporate Travel").pk
    assert services["wedding"] == ServiceType.objects.get(name="Wedding Transportation").pk


# --- the no-JS fallback ------------------------------------------------------------


@pytest.mark.parametrize(
    "slug,name",
    [
        ("airport", "Airport Transfer"),
        ("corporate", "Corporate Travel"),
        ("wedding", "Wedding Transportation"),
    ],
)
def test_the_service_query_string_preselects_that_occasion(client, slug, name):
    resp = client.get(f"/bookings/?service={slug}")
    assert resp.status_code == 200
    assert resp.context["form"]["service_type"].value() == ServiceType.objects.get(name=name).pk


def test_an_unknown_service_slug_preselects_nothing_and_does_not_crash(client):
    resp = client.get("/bookings/?service=nonsense")
    assert resp.status_code == 200
    assert not resp.context["form"]["service_type"].value()


def test_a_retired_occasion_is_not_preselected(client):
    ServiceType.objects.filter(name="Airport Transfer").update(active=False)
    resp = client.get("/bookings/?service=airport")
    assert resp.status_code == 200
    assert not resp.context["form"]["service_type"].value()


def test_the_preselected_occasion_reaches_the_rendered_widget(client):
    airport = ServiceType.objects.get(name="Airport Transfer")
    html = client.get("/bookings/?service=airport").content.decode()
    select = re.search(r'<select[^>]*name="service_type".*?</select>', html, re.S).group(0)
    assert f'value="{airport.pk}" selected' in select or f'value="{airport.pk}"  selected' in select


def test_posting_a_booking_is_unaffected_by_the_query_string(client):
    from .test_booking import VALID

    assert client.post("/bookings/?service=airport", VALID).status_code == 302
