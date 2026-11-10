"""The shared modal knows four variants: info, success, danger, gold (CLAUDE.md, modal.html).
Anything else falls back to the info icon silently — Retry balance passed 'warning' and
showed an info circle on a money action (e2e handoff finding 17)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VARIANTS = {"info", "success", "danger", "gold"}
LITERAL = re.compile(r"""variant:\s*['"]([a-z]+)['"]""")


def _sources():
    yield ROOT / "static" / "js" / "app.js"
    yield from (ROOT / "templates").rglob("*.html")


def test_every_literal_modal_variant_is_one_the_modal_draws():
    bad = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in _sources()
        for name in LITERAL.findall(path.read_text())
        if name not in VARIANTS
    ]
    assert bad == []
