"""The shared searchable select, and the two things coverage controls need from it.

`templates/components/searchable_select.html` is the only dropdown in the app — a native
`<select>` is forbidden (CLAUDE.md) and the reservation editor has a test that enforces it.
Coverage controls (APC-48) need two things it could not do:

* **A row that is more than a label.** Picking an affiliate means weighing their insurance
  standing and whether they are on GNet. Losing that to fit a dropdown would make the
  picker prettier and the decision worse.
* **Create-on-type that reaches the server.** Tom Select's own `create` invents a
  client-side option whose value is the typed text. An affiliate's driver has to become a
  real `VendorDriver` row, so the create has to post.

Both are added to the shared component rather than hand-rolled in the fragment, because
the next picker that needs them should not have to rebuild them.
"""

import json
from pathlib import Path

from django.template.loader import render_to_string

ROOT = Path(__file__).resolve().parents[3]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


def _render(**ctx) -> str:
    return render_to_string("components/searchable_select.html", ctx)


# --- the plain form still works exactly as it did ------------------------------------


def test_plain_options_render_as_before():
    html = _render(name="channel", options=[("phone", "Phone"), ("web", "Web")], selected="web")
    assert 'name="channel"' in html
    assert "data-tom" in html
    assert '<option value="web" selected>Web</option>' in html


def test_an_empty_label_is_the_first_option():
    html = _render(name="t", options=[("a", "A")], empty_label="All channels")
    assert html.index("All channels") < html.index(">A<")


# --- rich options ---------------------------------------------------------------------


def _rich():
    return [
        {
            "value": 7,
            "label": "Reston Coach Co",
            "sub": "Northern Virginia · used 12×",
            "badge": "GNET",
            # anything else the page needs rides at the top level, alongside the display
            # keys — it all lands in Tom Select's option data together
            "email": "ops@reston.example",
            "gnet": True,
        },
        {"value": 8, "label": "Beltway Executive", "sub": "Insurance lapsed", "warn": True},
    ]


def test_a_rich_option_keeps_its_value_and_label():
    html = _render(name="vendor", rich_options=_rich())
    assert 'value="7"' in html
    assert "Reston Coach Co" in html


def test_a_rich_option_carries_its_detail_to_tom_select():
    """Tom Select reads `data-data` as the option's data — that is how the sub-line, the
    badge and the email/GNet guard survive being put in a dropdown."""
    html = _render(name="vendor", rich_options=_rich())
    assert "data-data=" in html
    assert "data-rich" in html  # tells initTomSelects to use the rich renderer
    # the JSON is HTML-escaped into the attribute, so it must decode back to real JSON
    import html as html_mod
    import re

    raw = re.search(r'data-data="([^"]*)"', html).group(1)
    parsed = json.loads(html_mod.unescape(raw))
    assert parsed["sub"] == "Northern Virginia · used 12×"
    assert parsed["badge"] == "GNET"
    assert parsed["email"] == "ops@reston.example"
    assert parsed["gnet"] is True


def test_a_rich_option_is_selectable():
    html = _render(name="vendor", rich_options=_rich(), selected=8)
    assert '<option value="8" selected' in html


def test_a_quote_in_a_label_cannot_break_the_attribute():
    """The same class of bug as the Alpine x-data one: a raw `"` ends the attribute."""
    rich = [{"value": 1, "label": 'Bob "The Bus" Ryan', "sub": 'say "hello"'}]
    html = _render(name="driver", rich_options=rich)
    import re

    raw = re.search(r'data-data="([^"]*)"', html).group(1)
    assert '"' not in raw  # every quote entity-escaped
    import html as html_mod

    assert json.loads(html_mod.unescape(raw))["sub"] == 'say "hello"'


# --- create-on-type that posts --------------------------------------------------------


def test_a_create_url_marks_the_select_creatable():
    html = _render(name="driver", options=[], create_url="/portal/dispatch/vendor/3/drivers/")
    assert "data-create" in html
    assert 'data-create-url="/portal/dispatch/vendor/3/drivers/"' in html


def test_create_without_a_url_is_still_client_side_only():
    html = _render(name="x", options=[], create=1)
    assert "data-create" in html
    assert "data-create-url" not in html


# --- and the JS honours both ----------------------------------------------------------


def test_init_tom_selects_renders_the_rich_rows():
    block = APP_JS[APP_JS.index("function initTomSelects") :][:4200]
    assert "dataset.rich" in block
    assert "render" in block


def test_init_tom_selects_posts_a_created_option():
    block = APP_JS[APP_JS.index("function initTomSelects") :][:4200]
    assert "createUrl" in block
    assert "X-CSRFToken" in block
