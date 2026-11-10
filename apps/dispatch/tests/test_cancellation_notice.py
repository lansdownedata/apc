"""Telling an affiliate a trip is off.

There has always been an offer email and a T-48h confirmation, but nothing that says
"this is no longer yours". Reassigning a manual-channel trip left the dispatcher to ring
them — fine as a fallback, easy to forget, and the affiliate keeps the trip in their diary.

Trip-level by construction (an assignment is one reservation's), and carries no money at
all: an offer names what we pay, a cancellation has nothing to pay.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _assignment(**over):
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        rate=Decimal("1000"),
        hours=1,
        min_hours=0,
        passengers=9,
        stops=["Dulles International", "The Hay-Adams"],
    )
    over.setdefault("vendor", VendorFactory(name="Reston Coach Co", email="ops@reston.example"))
    return AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, **over)


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


# --- the notice itself ---------------------------------------------------------------


def test_it_tells_the_affiliate_the_trip_is_off(mailoutbox):
    a = _assignment()
    assert services.send_cancellation(a) is True
    (mail,) = mailoutbox
    assert mail.to == ["ops@reston.example"]
    assert "cancel" in mail.subject.lower()
    assert "Reston Coach Co" in mail.body


def test_it_carries_the_trip_so_they_know_which_one(mailoutbox):
    services.send_cancellation(_assignment())
    body = mailoutbox[0].body
    assert "Dulles International" in body
    assert "The Hay-Adams" in body


def test_it_names_no_money_at_all(mailoutbox):
    """An offer says what we pay. A cancellation has nothing to pay."""
    services.send_cancellation(_assignment(payout=Decimal("640.00")))
    body = mailoutbox[0].body
    for forbidden in ("640", "Payout", "payout", "$"):
        assert forbidden not in body, forbidden


def test_an_affiliate_with_no_email_is_not_a_crash(mailoutbox):
    a = _assignment(vendor=VendorFactory(name="No Mail Co", email=""))
    assert services.send_cancellation(a) is False
    assert mailoutbox == []


def test_in_house_coverage_has_nobody_to_tell(mailoutbox):
    a = _assignment(vendor=None, driver=DriverFactory(), payout=0)
    assert services.send_cancellation(a) is False
    assert mailoutbox == []


# --- triggering it -------------------------------------------------------------------


def test_the_endpoint_needs_login(client):
    a = _assignment()
    resp = client.post(reverse("dispatch_cancel_notice", args=[a.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_an_agent_can_send_it(client, agent, mailoutbox):
    a = _assignment()
    resp = client.post(reverse("dispatch_cancel_notice", args=[a.pk]))
    assert resp.json()["ok"] is True
    assert len(mailoutbox) == 1


def test_it_says_so_when_there_was_nobody_to_send_to(client, agent, mailoutbox):
    a = _assignment(vendor=VendorFactory(name="No Mail Co", email=""))
    body = client.post(reverse("dispatch_cancel_notice", args=[a.pk])).json()
    assert body["ok"] is False
    assert "email" in body["error"].lower()


def test_reassigning_offers_to_send_it(client, agent):
    """The dispatcher decides — some reassignments are the affiliate's own doing."""
    a = _assignment()
    body = client.get(reverse("dispatch_assign_panel", args=[a.reservation_id])).content.decode()
    assert reverse("dispatch_cancel_notice", args=[a.pk]) in body
    assert "cancelNoticeUrl" in body  # the confirm's tick box; its copy lives in app.js
    assert "Reston Coach Co" in body


def test_the_tick_box_is_on_by_default():
    """Forgetting leaves an affiliate holding a trip that is no longer theirs."""
    from pathlib import Path as _P

    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    box = js[js.index("apc-cancel-notice") - 400 :][:600]
    assert "checked" in box


def test_the_notice_goes_out_before_the_release():
    """`withdraw` reloads the page, so sending afterwards would never happen."""
    from pathlib import Path as _P

    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    body = js[js.index("confirmWithdraw(url, copy") :][:1800]
    assert body.index("this.notify(") < body.index('this.send(url, { action: "withdraw" })')


def test_a_failed_notice_never_blocks_the_reassignment():
    from pathlib import Path as _P

    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    notify = js[js.index("async notify(url)") :][:500]
    assert "catch" in notify


def test_a_gnet_assignment_does_not_offer_it(client, agent):
    """The gateway release tells them; a second message would contradict nothing."""
    a = _assignment(channel=Assignment.Channel.GNET)
    body = client.get(reverse("dispatch_assign_panel", args=[a.reservation_id])).content.decode()
    assert reverse("dispatch_cancel_notice", args=[a.pk]) not in body


# The editor renders the very same fragment (APC-48), so there is no second tick box to
# keep in step — but it is fetched from its own endpoint, so that endpoint is checked too.


def _editor_fragment(client, assignment) -> str:
    url = reverse("dispatch_coverage_controls", args=[assignment.reservation_id])
    return client.get(url).content.decode()


def test_the_editor_offers_the_same_tick_box(client, agent):
    a = _assignment()
    assert reverse("dispatch_cancel_notice", args=[a.pk]) in _editor_fragment(client, a)


def test_the_editor_does_not_offer_it_for_gnet(client, agent):
    a = _assignment(channel=Assignment.Channel.GNET)
    assert reverse("dispatch_cancel_notice", args=[a.pk]) not in _editor_fragment(client, a)


def test_the_editor_does_not_offer_it_in_house(client, agent):
    a = _assignment(vendor=None, driver=DriverFactory(), payout=0)
    assert reverse("dispatch_cancel_notice", args=[a.pk]) not in _editor_fragment(client, a)


def test_both_surfaces_get_it_from_one_place(client, agent):
    """The drawer and the editor cannot disagree about this, because it is one fragment."""
    a = _assignment()
    panel = client.get(reverse("dispatch_assign_panel", args=[a.reservation_id]))
    assert 'include "dispatch/_coverage_controls.html"' in _panel_source()
    assert reverse("dispatch_cancel_notice", args=[a.pk]) in panel.content.decode()


def _panel_source() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return (root / "templates" / "dispatch" / "_assign_panel.html").read_text()


def test_reassigning_keeps_the_drawer_open_on_the_same_trip():
    """Reassign means "pick someone else" — reloading the page closed the drawer on them.

    A withdraw puts this trip's fresh panel back into the drawer instead, and flags the
    drawer so closing it re-reads the page behind (the board row is stale until then).
    """
    from pathlib import Path as _P

    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    send = js[js.index("async send(url, extra)") :][:1600]
    assert "drawer-open" in send
    assert "stale: true" in send
    start = js.index("function drawer()")
    drawer = js[start : js.index("window.drawer = drawer", start)]
    assert "this.stale" in drawer
    assert "window.location.reload()" in drawer


def test_the_panel_knows_its_own_url(client, agent):
    a = _assignment()
    body = client.get(reverse("dispatch_assign_panel", args=[a.reservation_id])).content.decode()
    assert f"assignPanel('{reverse('dispatch_assign_panel', args=[a.reservation_id])}')" in body


def test_after_a_reassign_the_drawer_scrolls_to_choosing_who_covers_it(client, agent):
    """The pick-someone-else controls sit below the trip sheet, off-screen on a laptop."""
    from pathlib import Path as _P

    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    send = js[js.index("async send(url, extra)") :][:1600]
    assert 'scrollTo: "[data-cover-pick]"' in send
    start = js.index("function drawer()")
    drawer = js[start : js.index("window.drawer = drawer", start)]
    assert "scrollIntoView" in drawer
    trip = _assignment().reservation
    trip.assignments.update(status=Assignment.Status.WITHDRAWN)
    body = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    assert "data-cover-pick" in body


# --- DSP-05: a failed notice survives the reassign's reload ----------------------------
# Reassign sends the notice, then withdraws, and the withdraw reloads the page. The notice
# failure used to exist only as a client toast, so the reload wiped it before anyone read
# it: the dispatcher believed the affiliate had been emailed. The failure is now also a
# flash message, which the reloaded page shows (components/flash_messages.html).


def _reassign(client, assignment) -> int:
    """What confirmReassign does with the box ticked: notice first, then withdraw."""
    notice = client.post(reverse("dispatch_cancel_notice", args=[assignment.pk]))
    withdraw = client.post(
        reverse("dispatch_resolve", args=[assignment.pk]), {"action": "withdraw"}
    )
    assert withdraw.json()["ok"] is True, "a failed notice never blocks the reassignment"
    return notice.status_code


def _flashes(client) -> str:
    body = client.get(reverse("dispatch_board")).content.decode()
    start = body.find('<template id="flash-messages">')
    return body[start : body.index("</template>", start)] if start != -1 else ""


def test_no_email_on_file_is_still_on_screen_after_the_reload(client, agent, mailoutbox):
    a = _assignment(vendor=VendorFactory(name="No Mail Co", email=""))
    assert _reassign(client, a) == 400
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN
    flashes = _flashes(client)
    assert 'data-level="error"' in flashes
    assert "No email on file for that affiliate — call them." in flashes
    assert mailoutbox == []


def test_outbound_off_is_still_on_screen_after_the_reload(client, agent, settings, mailoutbox):
    settings.OUTBOUND_ENABLED = False
    a = _assignment()
    assert _reassign(client, a) == 409
    assert 'data-level="error"' in _flashes(client)
    assert mailoutbox == []


def test_a_sent_notice_leaves_no_error_behind(client, agent, mailoutbox):
    a = _assignment()
    assert _reassign(client, a) == 200
    assert len(mailoutbox) == 1
    assert 'data-level="error"' not in _flashes(client)


# The browser side of the same flow. There is no JS test runner in the repo: this runs the
# real coverageControls.confirmReassign under node with the modal, fetch, the tick box and
# the reload stubbed, and records what it posts. It covers the order of the two posts and
# that the reload still happens after a failed notice; the flash itself is covered above.

_REASSIGN_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const posted = [];
const toasts = [];
let modal = null;
let reloaded = 0;
class FormData { set() {} }
const ctx = {
  FormData,
  getCookie: () => "csrf",
  escapeHtml: (s) => s,
  document: { getElementById: () => ({ checked: input.ticked }) },
  window: { location: { reload: () => { reloaded += 1; } } },
  Alpine: { store: (name) => (name === "modal"
    ? { show: (o) => { modal = o; } }
    : { push: (t) => toasts.push(t.title) }) },
  fetch: async (url) => {
    posted.push(url);
    const body = url === "/notice/" ? input.notice : { ok: true };
    return { json: async () => body };
  },
};
vm.createContext(ctx);
vm.runInContext(input.source + `
  const c = coverageControls({});
  c.$el = { closest: () => null };
  c.confirmReassign("/resolve/", { cancelNoticeUrl: "/notice/", who: "Reston" });
`, ctx);
(async () => {
  await modal.onConfirm();
  process.stdout.write(JSON.stringify({ posted, toasts, reloaded }));
})();
"""


def _confirm_reassign(ticked: bool, notice: dict) -> dict:
    import json
    import shutil
    import subprocess
    from pathlib import Path as _P

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    js = (_P(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index("function coverageControls(")
    source = js[start : js.index("window.coverageControls = coverageControls;", start)]
    result = subprocess.run(
        ["node", "-e", _REASSIGN_HARNESS],
        input=json.dumps({"source": source, "ticked": ticked, "notice": notice}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_ticked_reassign_sends_the_notice_then_withdraws():
    out = _confirm_reassign(True, {"ok": True})
    assert out == {"posted": ["/notice/", "/resolve/"], "toasts": [], "reloaded": 1}


def test_unticked_reassign_only_withdraws():
    out = _confirm_reassign(False, {"ok": True})
    assert out == {"posted": ["/resolve/"], "toasts": [], "reloaded": 1}


def test_a_failed_notice_still_withdraws_and_reloads():
    error = "No email on file for that affiliate — call them."
    out = _confirm_reassign(True, {"ok": False, "error": error})
    assert out == {"posted": ["/notice/", "/resolve/"], "toasts": [error], "reloaded": 1}
