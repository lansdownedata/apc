"""Guards against multi-line `{# … #}` comments leaking into rendered pages.

Django's comment lexer regex is not `re.DOTALL`, so a `{# … #}` that spans more
than one line is never stripped: its body renders as literal text on the page (and
any markup inside it becomes real HTML). Three of these shipped to the dispatch
board and the assign drawer, where paragraphs of developer commentary rendered
above the trip table.

`{% comment %}…{% endcomment %}` is the multi-line form and has no such limit.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIRS = [ROOT / "templates", *sorted(ROOT.glob("apps/*/templates"))]
SUFFIXES = {".html", ".txt"}


def _templates() -> list[Path]:
    return sorted(
        p for d in TEMPLATE_DIRS for p in d.rglob("*") if p.is_file() and p.suffix in SUFFIXES
    )


def _multiline_comments(text: str) -> list[int]:
    """1-indexed start lines of every `{# … #}` whose body spans a newline."""
    offenders: list[int] = []
    i = 0
    while (start := text.find("{#", i)) != -1:
        end = text.find("#}", start)
        if end == -1:  # unclosed — the rest of the file renders literally
            offenders.append(text.count("\n", 0, start) + 1)
            break
        if "\n" in text[start:end]:
            offenders.append(text.count("\n", 0, start) + 1)
        i = end + 2
    return offenders


def test_templates_are_discovered():
    """Sanity check that the scan below is actually looking at something."""
    found = _templates()
    assert len(found) >= 100, f"expected the template tree, found {len(found)} files"


def test_no_template_has_a_multiline_django_comment():
    offenders = [
        f"{p.relative_to(ROOT)}:{line}"
        for p in _templates()
        for line in _multiline_comments(p.read_text())
    ]
    assert not offenders, (
        "multi-line `{# … #}` renders as literal page text — "
        f"use `{{% comment %}}…{{% endcomment %}}` instead: {offenders}"
    )
