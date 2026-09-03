"""APC-14 — the quote workspace's treatment of a linked set of identical trips.

A set is four rows in the database and one line on the screen. These pin that collapse,
the editor's quantity control, and the "apply to all in group" affordance. The JS half is
asserted by reading `static/js/app.js` as a file (same approach as the flight-verify and
icon tests) — there is no JS test harness in this repo.
"""

import json
import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.reservations import groups
from apps.reservations.factories import TransferReservationFactory

ROOT = Path(__file__).resolve().parents[3]
EDITOR = ROOT / "templates" / "leads" / "_reservation_editor.html"
DETAIL = ROOT / "templates" / "leads" / "lead_detail.html"
APP_JS = ROOT / "static" / "js" / "app.js"

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())
    return client


def _html(client, lead) -> str:
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


def _drafts(html: str) -> list[dict]:
    payload = re.search(
        r'<script id="ws-reservations" type="application/json">(.*?)</script>', html, re.S
    )
    return json.loads(payload.group(1))


# --- the workspace collapses a set into one line ---------------------------------------


def test_a_set_of_four_renders_as_one_line(agent):
    lead = LeadFactory()
    alone = TransferReservationFactory(lead=lead)
    coaches = TransferReservationFactory(lead=lead)
    groups.set_group_size(coaches, 4)

    html = _html(agent, lead)

    assert re.findall(r'data-line="(\d+)"', html) == [str(alone.pk), str(coaches.pk)]


def test_a_set_carries_a_multiplier_badge(agent):
    res = TransferReservationFactory()
    groups.set_group_size(res, 4)

    assert "×4" in _html(agent, res.lead)


def test_a_lone_trip_gets_no_multiplier_badge(agent):
    res = TransferReservationFactory()

    assert "×1" not in _html(agent, res.lead)


def test_a_set_line_prices_the_whole_set(agent):
    res = TransferReservationFactory(rate=200, hours=1, gratuity_pct=0)
    groups.set_group_size(res, 3)
    res.refresh_from_db()

    html = _html(agent, res.lead)

    assert f"${res.line_total * 3:,.2f}" in html


def test_every_member_of_a_set_is_reachable_from_the_expanded_line(agent):
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()

    html = _html(agent, res.lead)

    for member in res.lead.reservations.all():
        assert f"editReservation({member.pk})" in html


def test_a_set_line_removes_the_whole_set(agent):
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)

    html = _html(agent, res.lead)

    assert reverse("reservation_group_delete", args=[res.pk]) in html


def test_a_lone_trip_still_uses_the_single_trip_delete(agent):
    res = TransferReservationFactory()

    html = _html(agent, res.lead)

    assert reverse("reservation_delete", args=[res.pk]) in html
    assert reverse("reservation_group_delete", args=[res.pk]) not in html


# --- the editor knows a draft's set size -----------------------------------------------


def test_each_draft_carries_its_set_size(agent):
    lead = LeadFactory()
    alone = TransferReservationFactory(lead=lead)
    coaches = TransferReservationFactory(lead=lead)
    groups.set_group_size(coaches, 4)

    sizes = {d["id"]: d["quantity"] for d in _drafts(_html(agent, lead))}

    assert sizes[alone.pk] == 1
    assert all(sizes[m.pk] == 4 for m in lead.reservations.exclude(pk=alone.pk))


def test_the_editor_has_a_quantity_input_bound_to_the_draft():
    editor = EDITOR.read_text()
    assert "draft.quantity" in editor
    assert re.search(r'x-model\.number="draft\.quantity"[^>]*type="number"', editor)


def test_the_quantity_input_is_capped_at_the_service_limit(agent):
    """The cap comes from the service constant, not a number typed into the template."""
    res = TransferReservationFactory()

    assert f'max="{groups.DUPLICATE_MAX}"' in _html(agent, res.lead)


def test_the_editor_offers_apply_to_all_only_for_a_trip_already_in_a_set():
    editor = EDITOR.read_text()
    assert "applyToGroup" in editor
    assert re.search(r'x-show="groupSizeAtOpen > 1"', editor)


def test_the_editor_uses_no_native_select_or_confirm():
    """CLAUDE.md's hard UI rule. Comments talk about `<select>` — markup must not use one."""
    markup = re.sub(r"\{#.*?#\}", "", EDITOR.read_text())
    assert "window.confirm" not in markup
    assert not re.search(r"<select\s+(?![^>]*data-tom)", markup)


# --- the JS wiring ---------------------------------------------------------------------


def test_a_new_draft_starts_at_one_vehicle():
    assert re.search(r"quantity:\s*1", APP_JS.read_text())


def test_opening_a_saved_trip_remembers_the_set_size_it_started_at():
    js = APP_JS.read_text()
    assert "groupSizeAtOpen" in js
    assert re.search(r"this\.groupSizeAtOpen = .*draft\.quantity", js)


def test_the_save_payload_carries_the_quantity_and_the_apply_flag():
    js = APP_JS.read_text()
    assert re.search(r"d\.applyToGroup = ", js)
    assert "d.quantity" in js


def test_shrinking_a_set_asks_before_deleting_trips():
    """Removing vehicles destroys reservations that may already be assigned — the shared
    modal gates it, never window.confirm."""
    js = APP_JS.read_text()
    assert not re.search(r"\bwindow\.confirm\s*\(", js)  # comments may name it; nothing calls it
    assert re.search(r"quantity[\s\S]{0,900}?Alpine\.store\(\"modal\"\)\.confirm", js)


def test_removing_a_whole_set_is_gated_by_the_shared_modal():
    detail = DETAIL.read_text()
    assert "$store.modal.confirm" in detail
    assert "form-group-del-" in detail
