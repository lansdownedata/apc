"""Tests for the `json_filters` template tags."""

from django.template import Context, Template

from apps.core.templatetags.json_filters import json_string


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
