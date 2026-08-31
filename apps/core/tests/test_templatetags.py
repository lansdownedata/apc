"""Tests for the `json_filters` template tags."""

from datetime import date, time

import pytest
from django.template import Context, Template

from apps.core.templatetags.json_filters import json_attr, json_string
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def test_json_string_is_html_attribute_safe():
    # Embedded in a double-quoted HTML attribute (e.g. Alpine x-data), the JSON
    # string's own quotes must be entity-escaped so they don't close the attribute.
    out = json_string("Smoke Tester")
    assert '"' not in out
    assert out == "&quot;Smoke Tester&quot;"


def test_json_string_escapes_embedded_quote():
    out = json_string('Jon "JJ"')
    assert '"' not in out
    assert "&quot;" in out


def test_json_string_none_is_empty_js_string():
    assert json_string(None) == "&quot;&quot;"


def test_x_data_attribute_is_not_truncated():
    # A double-quoted x-data attribute must contain exactly two raw double-quotes
    # (its own delimiters); every inner quote is entity-escaped.
    tpl = Template('{% load json_filters %}<div x-data="f({ name: {{ n|json_string }} })"></div>')
    html = tpl.render(Context({"n": 'A "quoted" name'}))
    assert html.count('"') == 2


def test_json_string_escapes_quotes_for_attributes():
    assert json_string('a"b') == "&quot;a\\&quot;b&quot;"


def test_json_attr_encodes_dicts_and_escapes_for_attributes():
    out = json_attr({"airport": 3, "flight": '12"3', "when": None})
    assert out.startswith("{&quot;airport&quot;: 3")
    assert "<" not in out and '"' not in out


def test_pickup_clock_omits_abbrev_in_the_project_zone():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="America/New_York",
    )
    tpl = Template("{% load time_filters %}{{ trip|pickup_clock }}")
    assert tpl.render(Context({"trip": res})) == "7:30 AM"


def test_pickup_clock_appends_pdt_for_a_pacific_trip():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="America/Los_Angeles",
    )
    tpl = Template("{% load time_filters %}{{ trip|pickup_clock }}")
    assert tpl.render(Context({"trip": res})) == "7:30 AM PDT"


def test_pickup_clock_labels_a_stop_time_with_the_trip_zone():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="America/Los_Angeles",
    )
    tpl = Template("{% load time_filters %}{{ trip|pickup_clock:when }}")
    assert tpl.render(Context({"trip": res, "when": time(14, 0)})) == "2:00 PM PDT"


def test_pickup_clock_empty_without_a_time():
    res = TransferReservationFactory(pickup_time=None, pickup_timezone="America/Los_Angeles")
    tpl = Template("{% load time_filters %}{{ trip|pickup_clock }}")
    assert tpl.render(Context({"trip": res})) == ""
