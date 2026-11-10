"""LEAD-14 / TRIP-10: the lead and order pages render their own flash messages inline, and
every one of them came out in the amber warning style with the warning icon — "Order
booked." read like something had gone wrong."""

from pathlib import Path

from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string

ROOT = Path(__file__).resolve().parents[3]


def _render(level: int, text: str) -> str:
    return render_to_string("components/inline_messages.html", {"messages": [Message(level, text)]})


def test_a_success_message_reads_as_success():
    html = _render(constants.SUCCESS, "Order booked.")
    assert "tone-ok" in html
    assert "ti-circle-check" in html
    assert "tone-warn" not in html
    assert "Order booked." in html


def test_an_error_stays_danger():
    html = _render(constants.ERROR, "Could not save.")
    assert "tone-danger" in html
    assert "ti-alert-triangle" in html


def test_a_warning_stays_amber():
    html = _render(constants.WARNING, "Check the card.")
    assert "tone-warn" in html


def test_lead_and_order_pages_use_the_shared_messages():
    for name in ("leads/lead_detail.html", "orders/order_detail.html"):
        src = (ROOT / "templates" / name).read_text()
        assert 'include "components/inline_messages.html"' in src, name
        assert "for message in messages" not in src, name


def test_the_success_tone_is_styled():
    css = (ROOT / "static" / "css" / "app.css").read_text()
    assert ".tone-ok " in css
