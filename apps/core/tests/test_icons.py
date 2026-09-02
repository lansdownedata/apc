"""The `{% icon %}` tag: inline Tabler SVG, Tailwind-styleable, one system site-wide.

Icons used to be a three-way mess — a render-blocking Tabler webfont, hand-drawn
`<svg>` paths pasted (and duplicated) across the public templates, and literal
Unicode dingbats (◆ ⚑ ● ▼) standing in for icons in the wedding flow. This tag
replaces all three: it reads a vendored Tabler SVG from `static/icons/` and
returns it inline with our classes injected, so `h-4 w-4 text-gold` actually
styles the glyph and there is no font to block first paint.

The vendored set is pinned to `@tabler/icons` 3.7.0 — the exact version the
webfont was on — so no portal icon silently changes shape. Add an icon by
copying its file out of `node_modules/@tabler/icons/icons/<variant>/`.
"""

import re
from pathlib import Path

import pytest
from django.template import Context, Template
from django.template.exceptions import TemplateSyntaxError

ROOT = Path(__file__).resolve().parents[3]
ICONS = ROOT / "static" / "icons"
TEMPLATES = ROOT / "templates"


def render(src: str, **ctx) -> str:
    return Template("{% load icons %}" + src).render(Context(ctx))


# --------------------------------------------------------------- the tag


def test_renders_inline_svg_with_the_path_data():
    out = render('{% icon "check" %}')
    assert out.strip().startswith("<svg")
    assert "</svg>" in out
    assert "<path" in out


def test_injects_the_class_and_drops_intrinsic_size():
    out = render('{% icon "check" class="h-4 w-4 text-gold" %}')
    assert 'class="h-4 w-4 text-gold"' in out
    # Tailwind h-*/w-* only win if the SVG's own width/height are gone.
    assert 'width="24"' not in out
    assert 'height="24"' not in out
    assert "viewBox=" in out


def test_is_hidden_from_assistive_tech_by_default():
    out = render('{% icon "check" %}')
    assert 'aria-hidden="true"' in out
    assert "<title>" not in out


def test_label_makes_it_a_labelled_image():
    out = render('{% icon "check" label="Booking confirmed" %}')
    assert 'role="img"' in out
    assert "<title>Booking confirmed</title>" in out
    assert "aria-hidden" not in out


def test_label_is_escaped():
    out = render('{% icon "check" label=evil %}', evil="<script>x</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_filled_variant_reads_the_filled_set():
    out = render('{% icon "star" variant="filled" %}')
    assert "<svg" in out
    assert 'fill="currentColor"' in out


def test_attrs_string_is_spliced_in_verbatim():
    # For attributes a template kwarg can't name: Alpine bindings, title, style.
    out = render('{% icon "check" attrs=raw %}', raw=':class="a" x-show="b"')
    assert ':class="a" x-show="b"' in out


def test_unknown_icon_is_a_loud_error():
    with pytest.raises(TemplateSyntaxError):
        render('{% icon "no-such-icon-anywhere" %}')


def test_tag_is_a_builtin_no_load_needed():
    # Registered in TEMPLATES OPTIONS builtins, so templates don't need {% load %}.
    assert "<svg" in Template('{% icon "check" %}').render(Context())


# --------------------------------------------------- the vendored set / guards

REQUIRED = ["check", "x", "diamond", "confetti", "calendar", "arrow-right"]


@pytest.mark.parametrize("name", REQUIRED)
def test_core_icons_are_vendored(name):
    assert (ICONS / "outline" / f"{name}.svg").exists()


def test_no_public_template_uses_the_old_icon_systems():
    """Public templates: no webfont `<i class="ti ...">`, no hand-rolled <svg>,
    no Unicode dingbats used as icons."""
    offenders = {}
    for p in (TEMPLATES / "public").rglob("*.html"):
        text = p.read_text()
        hits = []
        if "ti ti-" in text:
            hits.append("tabler webfont class")
        # A bare `<svg>` is a hand-rolled icon. The one allowed exception is an
        # Alpine-bound renderer (`<svg ... x-html="...">`) whose path data lives
        # in the JS icon map, not drawn by hand in the template.
        for match in re.finditer(r"<svg\b[^>]*>", text):
            if "x-html=" not in match.group(0):
                hits.append("inline <svg>")
                break
        for dingbat in ("&#9670;", "&#9873;", "&#9679;", "&#9660;"):
            if dingbat in text:
                hits.append(f"dingbat {dingbat}")
        if hits:
            offenders[str(p.relative_to(ROOT))] = hits
    assert not offenders, f"old icon systems still in public templates: {offenders}"


def test_public_shells_do_not_load_the_icon_webfont():
    shells = [
        "public/base_public.html",
        "public/pay.html",
        "public/quote.html",
        "public/deposit_success.html",
    ]
    for rel in shells:
        text = (TEMPLATES / rel).read_text()
        assert "@tabler/icons-webfont" not in text, f"{rel} still loads the Tabler webfont"
