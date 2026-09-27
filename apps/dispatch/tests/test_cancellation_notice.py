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
