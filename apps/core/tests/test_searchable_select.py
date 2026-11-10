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

import html as html_mod
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
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


# --- DSP-04: the rich rows actually draw their detail ----------------------------------
# Tom Select 2.3.1 only parses an option's JSON when `dataAttr` names it, and its default
# is null — so every rich row drew the vendor name alone. Its readData looks the setting up
# in `el.dataset`, which means the key is the dataset name ("data"), not the attribute name
# ("data-data", which silently parses nothing). No JS test runner exists in the repo: this
# runs the real initTomSelects under node, against the options Django actually rendered,
# with a stand-in TomSelect whose option parsing is transcribed from 2.3.1's getSettings
# (dist/js/tom-select.complete.js, `readData` in `init_select`). It does not load Tom
# Select itself, so a change in a future Tom Select version would not show up here.

_TOM_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const escape = (s) => String(s).replace(/[&<>"']/g, (c) => "&#" + c.charCodeAt(0) + ";");
const drawn = [];
class TomSelect {
  constructor(el, settings) {
    const attr = settings.dataAttr === undefined ? null : settings.dataAttr; // 2.3.1 default
    for (const opt of el.options) {
      let data = Object.assign({}, opt.dataset);
      const json = attr && data[attr];
      if (typeof json === "string" && json.length) data = Object.assign(data, JSON.parse(json));
      data.text = data.text || opt.textContent;
      data.value = data.value || opt.value;
      drawn.push(settings.render ? settings.render.option(data, escape) : escape(data.text));
    }
    el.tomselect = { control: null, control_input: null };
  }
}
const el = {
  dataset: input.dataset,
  options: input.options,
  hasAttribute: () => false,
};
const ctx = { TomSelect, document: { querySelectorAll: () => [el] } };
vm.createContext(ctx);
vm.runInContext(input.source + "\\ninitTomSelects();", ctx);
process.stdout.write(JSON.stringify(drawn));
"""


def _init_tom_selects_source() -> str:
    start = APP_JS.index("function initTomSelects(")
    return APP_JS[start : APP_JS.index('document.addEventListener("DOMContentLoaded"', start)]


def _draw_rows(html: str) -> list[str]:
    """What initTomSelects draws for each option of `html`, as a browser would read it."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    select = re.search(r"<select([^>]*)>", html).group(1)
    dataset = {"tom": "", **({"rich": ""} if re.search(r"\sdata-rich\b", select) else {})}
    options = []
    for value, attrs, text in re.findall(r'<option value="([^"]*)"([^>]*)>([^<]*)<', html):
        # the browser's `option.dataset`: `data-data` decoded from the attribute, as "data"
        raw = re.search(r'data-data="([^"]*)"', attrs)
        options.append(
            {
                "value": value,
                "textContent": html_mod.unescape(text.strip()),
                "dataset": {"data": html_mod.unescape(raw.group(1))} if raw else {},
            }
        )
    result = subprocess.run(
        ["node", "-e", _TOM_HARNESS],
        input=json.dumps(
            {"source": _init_tom_selects_source(), "dataset": dataset, "options": options}
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_rich_row_draws_its_alert_badge_and_sub_line():
    rich = [
        {
            "value": 3,
            "label": "Reston Coach Co",
            "sub": "used 4× · Valid · exp Dec '27",
            "badge": "GNET",
            "warn": False,
            "alert": "Expires before trip",
            "email": "ops@reston.example",
            "gnet": True,
        }
    ]
    (row,) = _draw_rows(_render(name="vendor", rich_options=rich))
    assert "Reston Coach Co" in row
    assert "Expires before trip" in row
    assert "GNET" in row
    assert "used 4×" in row


def test_a_plain_select_still_draws_just_its_label():
    rows = _draw_rows(_render(name="channel", options=[("phone", "Phone")]))
    assert rows == ["Phone"]
