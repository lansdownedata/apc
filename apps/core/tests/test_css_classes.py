"""Guards the compiled-Tailwind contract.

Tailwind is built ahead of time (tailwind.config.js -> static/css/tailwind.css,
see `npm run build:css`), so a class that doesn't resolve fails *silently*: the
markup keeps the class, the stylesheet just has no rule for it. Under the old
CDN this was equally silent, which is how `bg-gold-soft` and `hover:wash-gold`
shipped dead. These tests scan the sources and the compiled sheet rather than a
rendered page, because many shells are token-keyed and need fixtures to reach.

They also catch the new failure mode the build introduces: editing a template
without re-running `npm run build:css`.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
TAILWIND_CONFIG = ROOT / "tailwind.config.js"
TAILWIND_CSS = ROOT / "static" / "css" / "tailwind.css"

# Utility prefixes that take a colour token.
COLOR_PREFIXES = "|".join(
    (
        "bg",
        "text",
        "border",
        "ring",
        "ring-offset",
        "from",
        "via",
        "to",
        "fill",
        "stroke",
        "divide",
        "outline",
        "accent",
        "decoration",
        "shadow",
    )
)

# Roots of our brand palette. A class like `bg-gold-soft` looks brand-ish but the
# real token is `goldl`, so anything starting with one of these roots must match a
# key actually declared in the config.
BRAND_ROOTS = ("gold", "ink", "muted", "paper", "surface", "line", "charcoal", "onyx", "silver")


def _config_color_tokens() -> set[str]:
    """The colour keys declared in tailwind.config.js."""
    text = TAILWIND_CONFIG.read_text()
    block = re.search(r"colors:\s*\{(.+?)\n      \},", text, re.S)
    assert block, "could not locate the colors block in tailwind.config.js"
    return set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*):", block.group(1), re.M))


def _template_classes() -> set[str]:
    """Every whitespace-separated token appearing in a class attribute."""
    found: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        for attr in re.findall(r"""class\s*=\s*["']([^"']+)["']""", path.read_text()):
            for tok in attr.split():
                if tok and "{" not in tok and "}" not in tok:
                    found.add(tok)
    return found


def _css_escape(cls: str) -> str:
    """Escape a class name the way Tailwind writes it into the stylesheet."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "\\" + ch for ch in cls)


def test_config_palette_is_discovered():
    """Sanity check that the config parse below actually found the palette."""
    tokens = _config_color_tokens()
    assert {"gold", "goldl", "goldd", "ink", "surface"} <= tokens, tokens


def test_brand_colour_utilities_resolve_to_a_real_token():
    """No template may use a brand-ish colour token that the config doesn't define."""
    tokens = _config_color_tokens()
    pattern = re.compile(rf"^(?:[\w-]+:)*(?:{COLOR_PREFIXES})-([a-z][a-z0-9-]*)(?:/\d+)?$")

    offenders = set()
    for cls in _template_classes():
        m = pattern.match(cls)
        if not m:
            continue
        token = m.group(1)
        if token.startswith(BRAND_ROOTS) and token not in tokens:
            offenders.add(cls)

    assert not offenders, (
        f"brand colour utilities that resolve to nothing: {sorted(offenders)}. "
        f"Declared tokens are {sorted(tokens)}."
    )


@pytest.mark.parametrize(
    "rule",
    [".quote-form .field", ".quote-form textarea.field", ".qf-toggle"],
)
def test_quote_form_scope_is_present(rule):
    """The public quote form retunes .field inside its own scope so the shared
    portal .field is untouched. Written in app.css, not Tailwind — this guards
    against the scope being deleted, not against a missing build."""
    css = (ROOT / "static" / "css" / "app.css").read_text()
    assert rule in css, f"{rule!r} missing from app.css"


def test_quote_form_does_not_paint_the_tom_select_wrapper():
    """Tom Select copies the source <select>'s `.field` class onto .ts-wrapper. The
    global `.ts-wrapper.field` reset strips that padding, but `.quote-form .field`
    has the same specificity and is declared later, so it won it back — painting
    padding *around* .ts-control and insetting the Occasion box from every other
    field in the card. The wrapper is layout-only; .ts-control carries the look.
    """
    css = (ROOT / "static" / "css" / "app.css").read_text()
    block = re.search(r"\.quote-form \.ts-wrapper\.field \{(.+?)\}", css, re.S)
    assert block, "nothing re-strips the Tom Select wrapper inside .quote-form"
    assert "padding: 0" in block.group(1), (
        f"wrapper padding not cleared, so the select stays indented: {block.group(1)!r}"
    )


def test_selected_toggle_segment_carries_a_brand_colour():
    """The Transfer/Hourly toggle read as muted/disabled: the selected pill was
    white-on-near-white with plain body text, so neither the selection nor the
    brand registered. The checked segment must use a gold token."""
    css = (ROOT / "static" / "css" / "app.css").read_text()
    block = re.search(r"\.qf-seg input:checked \+ \.qf-seg-label \{(.+?)\}", css, re.S)
    assert block, "no rule for the checked toggle segment"
    assert "--gold" in block.group(1), (
        f"checked segment has no gold token, so it reads as muted: {block.group(1)!r}"
    )


def test_time_picker_number_wrapper_fills_the_row():
    """Stepping the time with the arrows exposed a white strip: .numInputWrapper is
    40px tall inside a 3.4rem (54.4px) row, so the calendar's white background showed
    below it. The wrapper must be pinned to the row height."""
    css = (ROOT / "static" / "css" / "app.css").read_text()
    block = re.search(r"\.flatpickr-time \.numInputWrapper \{(.+?)\}", css, re.S)
    assert block, "no height rule for .flatpickr-time .numInputWrapper"
    assert "3.4rem" in block.group(1), (
        f"wrapper not pinned to the 3.4rem row height: {block.group(1)!r}"
    )


def test_native_mobile_picker_cannot_outgrow_its_container():
    """On a phone flatpickr steps aside for the OS picker: it hides the styled
    altInput and swaps in a native date/time input classed `.flatpickr-mobile`
    (flatpickr.js `setupMobile`). iOS gives those a UA intrinsic width that beats
    `width: 100%` from `.field`, so both pickers hung out past the quote card.
    """
    css = (ROOT / "static" / "css" / "app.css").read_text()
    block = re.search(r"\.flatpickr-mobile \{(.+?)\}", css, re.S)
    assert block, "no width rule for .flatpickr-mobile"
    body = block.group(1)
    assert "min-width: 0" in body, f"UA intrinsic minimum not cleared: {body!r}"
    assert "max-width: 100%" in body, f"nothing caps the field at its container: {body!r}"


@pytest.mark.parametrize("cls", ["wash-gold", "hover:wash-gold"])
def test_custom_utility_is_compiled(cls):
    """`wash-gold` is a real utility, so its variants must compile too.

    It is only ever used as `hover:wash-gold`; a plain CSS class in app.css cannot
    produce that, which is why it lives in the Tailwind config as a plugin.
    """
    css = TAILWIND_CSS.read_text()
    assert f".{_css_escape(cls)}" in css, (
        f"{cls!r} has no rule in the compiled stylesheet — "
        f"register it in tailwind.config.js and re-run `npm run build:css`"
    )
