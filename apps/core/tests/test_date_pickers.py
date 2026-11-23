"""Every date and time in the app is picked with flatpickr, on every device.

The browser's own picker ignores the charcoal/gold theme and looks different in every
browser, so a native `<input type=date|time>` is not allowed anywhere: public site,
customer portal or staff portal. flatpickr itself also falls back to the native input on
a phone or tablet unless told not to, which is how the wedding date step showed the
browser calendar even though its markup was already a flatpickr input.
"""

import re
from pathlib import Path

import pytest

from apps.settings.forms import TaskConfigForm

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()

NATIVE = "date|time|datetime-local|month|week"
NATIVE_ATTR = re.compile(rf"""type=["']({NATIVE})["']""")
NATIVE_WIDGET = re.compile(rf"""["']type["']\s*:\s*["']({NATIVE})["']""")
TEMPLATE_COMMENTS = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}", re.S)


def test_flatpickr_never_hands_off_to_the_native_picker():
    """Every picker spreads FP_BASE, so the switch lives there once."""
    base = re.search(r"const FP_BASE = \{(.+?)\};", APP_JS, re.S)
    assert base, "FP_BASE not found in app.js"
    assert re.search(r"disableMobile:\s*true", base.group(1)), (
        "FP_BASE must set disableMobile: true, or phones get the browser's picker"
    )


def test_every_flatpickr_call_uses_the_shared_base():
    """A picker built from its own options would skip disableMobile. Inline calendars
    are the exception: flatpickr never swaps an inline calendar for a native input."""
    for call in re.finditer(r"window\.flatpickr\([^,]+,\s*(\{[^}]*|\w+)", APP_JS):
        opts = call.group(1)
        if opts.startswith("{"):
            assert "...FP_BASE" in opts or "inline: true" in opts, opts
        else:
            assert opts in {"opts", "FP_TIME"}, opts


def _sources():
    for path in (ROOT / "templates").rglob("*.html"):
        yield path, TEMPLATE_COMMENTS.sub("", path.read_text())
    for path in (ROOT / "apps").rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        yield path, path.read_text()


def test_no_native_date_or_time_inputs_anywhere():
    offenders = [
        str(path.relative_to(ROOT))
        for path, text in _sources()
        if NATIVE_ATTR.search(text) or NATIVE_WIDGET.search(text)
    ]
    assert offenders == []


@pytest.mark.django_db
def test_hold_time_setting_uses_the_flatpickr_time_picker():
    html = str(TaskConfigForm()["hold_place_time"])
    assert "data-flatpickr-time" in html
    assert 'type="time"' not in html
