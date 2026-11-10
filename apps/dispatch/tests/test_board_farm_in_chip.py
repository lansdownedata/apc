"""APC-136 — the dispatch board's "N GNet requests" chip."""

import re

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import farm_in, farm_in_drawer
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload

pytestmark = pytest.mark.django_db


def _arrive(tid: str, desk: str) -> None:
    farm_in.handle_new(sample_payload(), transaction_id=tid, delivery_id="d")
    FarmInRequest.objects.filter(transaction_id=tid).update(desk=desk)


def _board(client) -> str:
    client.force_login(UserFactory())
    resp = client.get(reverse("dispatch_board"))
    assert resp.status_code == 200
    return resp.content.decode()


def test_board_chip_count_matches_drawer_dispatch_filter(client):
    _arrive("c1", FarmInRequest.Desk.DISPATCH)
    _arrive("c2", FarmInRequest.Desk.DISPATCH)
    _arrive("c3", FarmInRequest.Desk.RESERVATIONS)
    page = _board(client)
    assert re.search(r"data-farm-in-chip[^>]*>\s*2 GNet requests", page)
    assert "farm-in-open" in page
    assert len(farm_in_drawer.rows(tab="pending", dispatch_only=True)) == 2


def test_board_chip_is_hidden_when_none(client):
    """Rendered but hidden, so a request sent to dispatch from the drawer can show it
    without a reload (GNET-08)."""
    _arrive("c4", FarmInRequest.Desk.RESERVATIONS)
    chip = re.search(r"<button[^>]*data-farm-in-chip[^>]*>", _board(client)).group(0)
    assert "x-cloak" in chip


def test_one_request_reads_singular(client):
    """GNET-08: "1 GNet requests"."""
    _arrive("c5", FarmInRequest.Desk.DISPATCH)
    page = _board(client)
    assert re.search(r"data-farm-in-chip[^>]*>\s*1 GNet request<", page)


def test_chip_follows_the_drawer_dispatch_count(client):
    """GNET-08: after Send back the chip kept its count until a reload. The drawer's body
    carries the dispatch count, and the chip listens for it."""
    _arrive("c6", FarmInRequest.Desk.DISPATCH)
    chip = re.search(r"<button[^>]*data-farm-in-chip[^>]*>", _board(client)).group(0)
    assert "@farm-in-dispatch-count.window" in chip

    client.force_login(UserFactory())
    body = client.get(reverse("farm_in_drawer"), {"tab": "pending"}).content.decode()
    assert 'data-dispatch="1"' in body


_LOAD_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const events = [];
const root = { dataset: { pending: "4", dispatch: "0" } };
const ctx = {
  root,
  URLSearchParams,
  CustomEvent: class { constructor(type, o) { this.type = type; this.detail = o.detail; } },
  window: { dispatchEvent: (e) => events.push([e.type, e.detail]) },
  Alpine: { store: () => ({ push() {} }) },
  fetch: async () => ({ ok: true, text: async () => "<div data-fi-root></div>" }),
};
vm.createContext(ctx);
const d = vm.runInContext(input.source + `
  const d = farmInDrawer();
  d.$root = { dataset: { url: "/fi/" } };
  d.$refs = { body: { querySelector: (s) => (s === "[data-fi-root]" ? root : null),
                      querySelectorAll: () => [] } };
  d.$nextTick = (fn) => fn();
  d;
`, ctx);
d.load().then(() => process.stdout.write(JSON.stringify(events)));
"""


def test_drawer_load_announces_the_dispatch_count():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index("function farmInDrawer(")
    source = js[start : js.index("window.farmInDrawer = farmInDrawer;", start)]
    result = subprocess.run(
        ["node", "-e", _LOAD_HARNESS],
        input=json.dumps({"source": source}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert ["farm-in-dispatch-count", 0] in json.loads(result.stdout)
