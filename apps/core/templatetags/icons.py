"""`{% icon "name" class="h-4 w-4 text-gold" %}` — one inline-SVG icon system.

Replaces the render-blocking Tabler webfont, the hand-drawn `<svg>` paths that
were pasted across the public templates, and the Unicode dingbats (◆ ⚑ ● ▼) the
wedding flow used as icons. The SVG is emitted inline so Tailwind `h-*/w-*` and
`text-*` (via `currentColor`) style it directly and nothing blocks first paint.

The set is vendored under `static/icons/{outline,filled}/`, pinned to
`@tabler/icons` 3.7.0 (the version the webfont was on, so no icon changes shape).
Add one by copying its file from `node_modules/@tabler/icons/icons/<variant>/`.

Registered as a template builtin (see `config/settings/base.py`), so templates
don't need `{% load icons %}`.
"""

import re
from functools import lru_cache
from pathlib import Path

from django import template
from django.conf import settings
from django.template.exceptions import TemplateSyntaxError
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

# Presentation attributes carried over from the source <svg>; width/height are
# dropped on purpose so the Tailwind size classes win.
_PRESENTATION = ("viewBox", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")
_NAME_RE = re.compile(r"[a-z0-9-]+")
_SEARCH_DIRS = tuple(Path(d) / "icons" for d in settings.STATICFILES_DIRS)


@lru_cache(maxsize=512)
def _load(name: str, variant: str) -> tuple[str, str]:
    """Return `(presentation-attr string, inner markup)` for a vendored icon."""
    if variant not in ("outline", "filled") or not _NAME_RE.fullmatch(name):
        raise KeyError(name)
    for base in _SEARCH_DIRS:
        path = base / variant / f"{name}.svg"
        if path.is_file():
            raw = path.read_text()
            break
    else:
        raise KeyError(name)

    open_end = raw.index(">", raw.index("<svg"))
    open_tag = raw[: open_end + 1]
    body = raw[open_end + 1 : raw.rindex("</svg>")].strip()

    attrs = []
    for attr in _PRESENTATION:
        match = re.search(rf'\s{re.escape(attr)}="([^"]*)"', open_tag)
        if match:
            attrs.append(f'{attr}="{match.group(1)}"')
    return " ".join(attrs), body


@register.simple_tag
def icon(name, variant="outline", label=None, attrs="", **kwargs):
    """Inline a vendored Tabler SVG.

    `class` sets Tailwind classes. `label` makes it a labelled image
    (`role="img"` + `<title>`) rather than decorative. `attrs` is a raw string
    spliced into the tag verbatim — for the attributes a template kwarg can't
    name, e.g. `attrs=':class="open ? ... : ..." x-show="open"'`.
    """
    try:
        presentation, body = _load(str(name), str(variant))
    except KeyError as exc:
        raise TemplateSyntaxError(
            f"icon: unknown icon {name!r} (variant {variant!r}) — vendor it into "
            f"static/icons/{variant}/ from node_modules/@tabler/icons/icons/"
        ) from exc

    if label:
        a11y = f'role="img" aria-label="{escape(label)}"'
        body = f"<title>{escape(label)}</title>{body}"
    else:
        a11y = 'aria-hidden="true"'

    cls = kwargs.get("class")
    class_attr = f' class="{escape(cls)}"' if cls else ""
    extra = f" {attrs}" if attrs else ""
    return mark_safe(
        f'<svg xmlns="http://www.w3.org/2000/svg" {presentation}{class_attr} '
        f"{a11y}{extra}>{body}</svg>"
    )
