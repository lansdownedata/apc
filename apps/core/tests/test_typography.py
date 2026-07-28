"""Guards the two-family type system: Fraunces for display, Inter for body/UI.

The brand kit (docs/brand/) specifies Inter as the body face. The portal and the
public site share the same tokens (static/css/app.css + the Tailwind config in
each shell template), so a face has to be declared consistently in every one of
them or a standalone page silently falls back to system-ui. These tests scan the
declarations themselves rather than a rendered page, because several of the
shells (deposit success/cancel, public quote) are token-keyed and not reachable
without fixtures.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
APP_CSS = ROOT / "static" / "css" / "app.css"

# Every template that ships its own <link> to Google Fonts is a standalone shell
# and must declare the full family set itself. Email templates are excluded on
# purpose: mail clients strip webfonts, so those deliberately body-set Georgia
# and pull Fraunces only as a best-effort display face.
FONT_SHELLS = sorted(
    p
    for p in TEMPLATES.rglob("*.html")
    if "fonts.googleapis.com/css2" in p.read_text() and "email" not in p.parts
)

RETIRED_FACES = ["Hanken Grotesk", "Hanken+Grotesk"]


def test_font_shells_are_discovered():
    """Sanity check that the scan below is actually looking at something."""
    assert len(FONT_SHELLS) >= 6, f"expected the known shells, found {FONT_SHELLS}"


@pytest.mark.parametrize("path", FONT_SHELLS, ids=lambda p: str(p.relative_to(ROOT)))
def test_shell_declares_inter_not_a_retired_face(path):
    """Each standalone shell loads Inter and no retired body face."""
    text = path.read_text()
    for face in RETIRED_FACES:
        assert face not in text, (
            f"{path.relative_to(ROOT)} still references retired body face {face!r}"
        )
    assert "Inter" in text, f"{path.relative_to(ROOT)} does not load Inter"


def test_app_css_body_face_is_inter():
    """The shared stylesheet sets Inter as the body/UI face, Fraunces as display."""
    css = APP_CSS.read_text()
    for face in RETIRED_FACES:
        assert face not in css, f"app.css still references retired body face {face!r}"
    assert "'Inter'" in css, "app.css does not declare Inter"
    assert "'Fraunces'" in css, "app.css lost its Fraunces display face"


def test_no_template_references_a_retired_face():
    """Catch stragglers anywhere in the template tree, not just the shells."""
    offenders = [
        str(p.relative_to(ROOT))
        for p in TEMPLATES.rglob("*.html")
        if any(face in p.read_text() for face in RETIRED_FACES)
    ]
    assert not offenders, f"retired body face still referenced in: {offenders}"
