"""Guards the two-family type system: Playfair Display for display, Inter for body/UI.

Fraunces was retired as the display face because its swash ampersand and merging
f-ligatures made the marketing hero headline unreadable ("Stress-free, reliable &
affordable"). Playfair Display is the face already in use on the client's live
site, so it also buys brand continuity.

The declarations live in five places — `tailwind.config.js`, `static/css/app.css`,
and an inline stack in each standalone shell — so a face has to be named in every
one of them or a page silently falls back to Georgia/system-ui. These tests scan
the declarations rather than a rendered page, because several shells (deposit
success/cancel, public quote) are token-keyed and unreachable without fixtures.

`static/css/tailwind.css` is compiled and committed, so it is checked too: a font
change that skips `npm run build:css` would ship stale CSS to prod.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
APP_CSS = ROOT / "static" / "css" / "app.css"
TAILWIND_CSS = ROOT / "static" / "css" / "tailwind.css"
TAILWIND_CONFIG = ROOT / "tailwind.config.js"

DISPLAY_FACE = "Playfair Display"
BODY_FACE = "Inter"

# Both the CSS spelling and the Google Fonts URL spelling (spaces become "+").
RETIRED_FACES = ["Hanken Grotesk", "Hanken+Grotesk", "Fraunces"]

# Every template that ships its own <link> to Google Fonts is a standalone shell
# and must declare the full family set itself. Email templates are excluded here
# on purpose: mail clients strip webfonts, so they body-set Georgia and pull the
# display face only as a best-effort enhancement. They are still covered by
# test_no_template_references_a_retired_face below.
FONT_SHELLS = sorted(
    p
    for p in TEMPLATES.rglob("*.html")
    if "fonts.googleapis.com/css2" in p.read_text() and "email" not in p.parts
)


def test_font_shells_are_discovered():
    """Sanity check that the scan below is actually looking at something."""
    assert len(FONT_SHELLS) >= 6, f"expected the known shells, found {FONT_SHELLS}"


@pytest.mark.parametrize("path", FONT_SHELLS, ids=lambda p: str(p.relative_to(ROOT)))
def test_shell_declares_both_faces_and_no_retired_face(path):
    """Each standalone shell loads the current display and body faces."""
    text = path.read_text()
    for face in RETIRED_FACES:
        assert face not in text, f"{path.relative_to(ROOT)} still references retired face {face!r}"
    assert BODY_FACE in text, f"{path.relative_to(ROOT)} does not load {BODY_FACE}"
    assert DISPLAY_FACE.replace(" ", "+") in text or DISPLAY_FACE in text, (
        f"{path.relative_to(ROOT)} does not load {DISPLAY_FACE}"
    )


def test_app_css_declares_current_faces():
    """The shared stylesheet sets Inter for body/UI and Playfair Display for display."""
    css = APP_CSS.read_text()
    for face in RETIRED_FACES:
        assert face not in css, f"app.css still references retired face {face!r}"
    assert f"'{BODY_FACE}'" in css, f"app.css does not declare {BODY_FACE}"
    assert f"'{DISPLAY_FACE}'" in css, f"app.css does not declare {DISPLAY_FACE}"


def test_tailwind_config_declares_current_faces():
    """tailwind.config.js is the single source of truth for the family tokens."""
    cfg = TAILWIND_CONFIG.read_text()
    for face in RETIRED_FACES:
        assert face not in cfg, f"tailwind.config.js still references retired face {face!r}"
    assert BODY_FACE in cfg, f"tailwind.config.js does not declare {BODY_FACE}"
    assert DISPLAY_FACE in cfg, f"tailwind.config.js does not declare {DISPLAY_FACE}"


def test_compiled_tailwind_css_is_rebuilt():
    """The committed build must reflect the config — i.e. `npm run build:css` was run."""
    built = TAILWIND_CSS.read_text()
    for face in RETIRED_FACES:
        assert face not in built, (
            f"static/css/tailwind.css still contains retired face {face!r} — "
            f"run `npm run build:css` and commit the result"
        )
    assert DISPLAY_FACE in built, (
        f"static/css/tailwind.css does not contain {DISPLAY_FACE} — "
        f"run `npm run build:css` and commit the result"
    )


def test_no_template_references_a_retired_face():
    """Catch stragglers anywhere in the template tree, including email templates."""
    offenders = [
        str(p.relative_to(ROOT))
        for p in TEMPLATES.rglob("*.html")
        if any(face in p.read_text() for face in RETIRED_FACES)
    ]
    assert not offenders, f"retired face still referenced in: {offenders}"
