"""APC-14 stage 3 — the wedding order form learns "this leg is several coaches".

A leg maps to a linked SET of trips now. The office picks how many; the couple never sees
a quantity control, only the plain-words count they were already shown. The JS half is
asserted by reading `static/js/app.js` as a file — there is no JS test harness here.
"""

import json
import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.public.wedding import vehicle_runs

ROOT = Path(__file__).resolve().parents[3]
ITINERARY = ROOT / "templates" / "public" / "_wedding_itinerary.html"
BUILDER = ROOT / "templates" / "leads" / "_wedding_builder.html"
APP_JS = ROOT / "static" / "js" / "app.js"

pytestmark = pytest.mark.django_db


_OFFICE_BLOCK = re.compile(r"\{%\s*if office\s*%\}(.*?)\{%\s*endif\s*%\}", re.S)


def _office_only(markup: str) -> str:
    """Just the parts of the shared itinerary the `{% if office %}` gate renders."""
    return "".join(_OFFICE_BLOCK.findall(markup))


def _outside_office(markup: str) -> str:
    """Everything the couple's own copy of the itinerary renders."""
    return _OFFICE_BLOCK.sub("", markup)


# --- the office picks the count --------------------------------------------------------


def test_the_office_itinerary_has_a_vehicles_input_for_the_leg():
    office = _office_only(ITINERARY.read_text())
    assert "setLegVehicles" in office
    assert re.search(r'type="number"[^>]*leg-vehicles', office) or re.search(
        r'leg-vehicles[^>]*type="number"', office
    )


def test_the_vehicles_input_is_office_only():
    """The public flow never assigns vehicles on purpose — and a couple must not be asked
    how many coaches to send. The count they see is derived, not typed."""
    markup = ITINERARY.read_text()
    assert "setLegVehicles" in _office_only(markup)
    public = _outside_office(markup)
    assert "setLegVehicles" not in public
    assert "legVehicles" not in public


def test_the_builder_posts_the_counts():
    builder = BUILDER.read_text()
    assert re.search(r'name="counts_json"[^>]*:value="countsJson"', builder)


def test_a_leg_needing_more_than_one_vehicle_is_badged():
    office = _office_only(ITINERARY.read_text())
    assert "legVehicles(leg) > 1" in office


def test_the_itinerary_uses_no_native_select_or_confirm():
    markup = re.sub(r"\{#.*?#\}", "", ITINERARY.read_text())
    markup = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", markup, flags=re.S)
    assert "window.confirm" not in markup
    assert not re.search(r"<select\s+(?![^>]*data-tom)", markup)


# --- the JS wiring ---------------------------------------------------------------------


def test_the_planner_derives_a_legs_vehicle_count():
    js = APP_JS.read_text()
    assert re.search(r"legVehicles\(leg\)\s*\{", js)
    assert "setLegVehicles" in js


def test_the_planner_mirrors_the_servers_coach_maths():
    """`vehicleRuns` in the browser and `vehicle_runs` on the server must agree, or the
    chip a couple reads disagrees with the trips the office gets."""
    js = APP_JS.read_text()
    assert re.search(r"vehicleRuns\(count\)\s*\{", js)
    assert "vehicle_runs" in js  # the mirror is named in the comment beside it


def test_the_planner_posts_a_counts_map():
    js = APP_JS.read_text()
    assert re.search(r"get countsJson\(\)\s*\{", js)


# --- end to end through the office form ------------------------------------------------


def test_saving_the_day_builds_the_coaches_each_leg_needs(client):
    from apps.public.tests.test_wedding_form import _legs, _post

    lead = LeadFactory()
    client.force_login(UserFactory())
    data = _post()
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)

    client.post(reverse("lead_wedding_save", args=[lead.pk]), data)

    legs = _legs()
    for leg in legs:
        members = lead.reservations.filter(source_leg_id=leg["id"])
        assert members.count() == vehicle_runs(leg["pax"], None)
        assert len({m.group_key for m in members}) == 1


def test_an_agents_count_overrides_the_derived_one_through_the_form(client):
    from apps.public.tests.test_wedding_form import _post

    lead = LeadFactory()
    client.force_login(UserFactory())
    data = _post()
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)
    data["counts_json"] = json.dumps({"guests-in": 5})

    client.post(reverse("lead_wedding_save", args=[lead.pk]), data)

    assert lead.reservations.filter(source_leg_id="guests-in").count() == 5


def test_the_multiplier_badge_is_legible_in_both_themes():
    """`charcoal` is a fixed hex in tailwind.config.js, not a theme token, so a charcoal
    chip vanishes into a dark card. The badge rides `gold`, which is a CSS variable and
    shifts with the theme, against fixed-dark text."""
    # Quote-aware: `x-show="legVehicles(leg) > 1"` puts a `>` inside an attribute, and a
    # naive `[^>]*` stops on it and truncates the tag. No slice literals in this file —
    # Tailwind's JIT scans apps/**/*.py and would emit one as a junk class.
    span = re.compile(r"""<span\b(?:[^>"']|"[^"]*"|'[^']*')*>""")
    for path in (ITINERARY, ROOT / "templates" / "leads" / "lead_detail.html"):
        flat = " ".join(path.read_text().split())
        badges = [
            m.group(0)
            for m in span.finditer(flat)
            if "\u00d7" in m.group(0) or flat.find("\u00d7", m.end(), m.end() + 24) != -1
        ]
        assert badges, f"no multiplier badge found in {path.name}"
        for tag in badges:
            assert "bg-gold" in tag, tag
            assert "bg-charcoal" not in tag, tag
