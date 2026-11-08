"""Accessibility guards that are cheap to check at source level."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _init_tom_selects() -> str:
    js = (ROOT / "static" / "js" / "app.js").read_text()
    start = js.index("function initTomSelects")
    end = js.index('document.addEventListener("DOMContentLoaded"', start)
    return js[start:end]


def test_tom_select_copies_the_selects_aria_label_to_its_control():
    body = _init_tom_selects()
    assert re.search(r'\["aria-label",\s*"aria-labelledby"\]', body)
    assert "hasAttribute(attr)" in body
    assert "setAttribute(attr" in body
    assert "control_input" in body
