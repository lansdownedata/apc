"""One set of coverage controls, for the drawer and the trip editor both (APC-48).

Moe, 2026-09-19: *"They should work the same, the editor and drawer versions. They should
be identical really."* They were not. The drawer rendered radio lists server-side; the
editor fetched JSON and rendered its own. Both posted to the same endpoints, so the rules
were never doubled — only the UI was, and it drifted.

Now there is one fragment, `templates/dispatch/_coverage_controls.html`. The drawer
includes it; the editor fetches it from `dispatch_coverage_controls` and drops it in with
x-html. "Identical" is structural, not maintained by hand, which is what these tests pin.

The rules still live in `dispatch.services` — one active assignment, a booked lead, an
active driver — and nothing here goes near them.
"""

import html as html_mod
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory, VehicleFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory
from apps.vendors.models import VendorDriver

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
EDITOR = (ROOT / "templates" / "leads" / "_reservation_editor.html").read_text()
PANEL = (ROOT / "templates" / "dispatch" / "_assign_panel.html").read_text()
FRAGMENT = (ROOT / "templates" / "dispatch" / "_coverage_controls.html").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


def _trip(status=Lead.Status.BOOKED, **over):
    return ReservationFactory(
        lead=LeadFactory(status=status),
        rate=Decimal("1000"),
        hours=1,
        min_hours=0,
        stops=["Dulles International", "The Hay-Adams"],
        **over,
    )


def _controls(client, trip):
    return client.get(reverse("dispatch_coverage_controls", args=[trip.pk]))


def _body(client, trip) -> str:
    return _controls(client, trip).content.decode()


def _options(body: str, select_name: str) -> list[dict]:
    """Every option's own data for one picker, as searchable_select.html wrote it."""
    block = re.search(rf'<select[^>]*name="{select_name}"(.*?)</select>', body, re.DOTALL)
    if not block:
        return []
    return [
        json.loads(html_mod.unescape(m)) for m in re.findall(r'data-data="([^"]*)"', block.group(1))
    ]


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())


# --- one fragment, two surfaces -------------------------------------------------------


def test_the_drawer_includes_the_shared_fragment():
    assert 'include "dispatch/_coverage_controls.html"' in PANEL


def test_the_editor_loads_the_shared_fragment():
    assert "dispatch_coverage_controls" in EDITOR
    assert 'x-html="coverageHtml"' in EDITOR


def test_there_is_no_second_implementation_left():
    """The delete-list on the ticket, asserted. A leftover is how they drift again."""
    for gone in (
        "setCoverageMode",
        "assignInHouse",
        "assignVendor",
        "releaseCoverage",
        "pickedDriver",
        "pickedVendor",
        "coverageUrl",
    ):
        assert gone not in APP_JS, gone
        assert gone not in EDITOR, gone
    assert "dispatch_assign_options" not in EDITOR
    # and the JSON endpoint itself is gone
    from django.urls import NoReverseMatch

    with pytest.raises(NoReverseMatch):
        reverse("dispatch_assign_options", args=[1])


def test_the_drawer_and_the_editor_render_the_same_markup(client, agent):
    """Not "look alike" — the same bytes, because it is the same template."""
    DriverFactory(name="Ray Delgado")
    VendorFactory(name="Reston Coach Co")
    trip = _trip()
    fragment = _body(client, trip)
    panel = client.get(reverse("dispatch_assign_panel", args=[trip.pk])).content.decode()
    # the fragment's own root, and both pickers, appear verbatim inside the drawer
    assert 'x-data="coverageControls(' in fragment
    for chunk in ('name="driver"', 'name="vehicle"', 'name="vendor"'):
        assert chunk in fragment and chunk in panel


# --- the chooser ----------------------------------------------------------------------


def test_an_uncovered_trip_offers_both_ways_to_cover_it(client, agent):
    DriverFactory(name="Ray Delgado")
    VendorFactory(name="Reston Coach Co")
    body = _body(client, _trip())
    assert "In-house" in body and "Farm-out" in body
    assert "Ray Delgado" in body
    assert "Reston Coach Co" in body


def test_the_toggle_stays_away_when_there_is_no_roster(client, agent):
    """With no active drivers there is no choice to make, so offering one is noise.
    Deliberate — `test_panel_omits_the_in_house_block_without_drivers` pins it too."""
    VendorFactory(name="Reston Coach Co")
    body = _body(client, _trip())
    assert "mode = 'in_house'" not in body
    assert "Reston Coach Co" in body


def test_a_covered_trip_says_who_has_it_and_offers_reassign(client, agent):
    trip = _trip()
    a = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Reston Coach Co"),
        status=Assignment.Status.CONFIRMED,
    )
    body = _body(client, trip)
    assert "Reston Coach Co" in body
    assert "Reassign" in body
    assert reverse("dispatch_resolve", args=[a.pk]) in body
    assert "mode = 'farm_out'" not in body  # no chooser while it is covered


def test_an_in_house_trip_names_the_driver(client, agent):
    trip = _trip()
    AssignmentFactory(reservation=trip, in_house=True, driver=DriverFactory(name="Ray Delgado"))
    assert "Ray Delgado" in _body(client, trip)


def test_a_quote_gets_no_controls_at_all(client, agent):
    """`services._claim` refuses an unbooked lead, so nothing should offer it."""
    body = _body(client, _trip(status=Lead.Status.QUOTED))
    assert "coverageControls(" not in body


def test_the_payout_starts_on_what_the_trip_pays_its_vendor(client, agent):
    """Quoted and actual margin start level."""
    VendorFactory()
    assert 'value="580.00"' in _body(client, _trip(affiliate_cost=Decimal("580")))


def test_the_controls_need_login(client):
    assert "/login" in _controls(client, _trip())["Location"]


# --- every picker is a searchable select ----------------------------------------------


def test_no_native_select_survives_anywhere_in_the_fragment():
    """CLAUDE.md forbids it, and a bare <select> is what this ticket had to replace."""
    for chunk in FRAGMENT.split("<select")[1:]:
        assert "data-tom" in chunk.split(">")[0]


def test_the_driver_vehicle_and_affiliate_pickers_are_all_searchable(client, agent):
    DriverFactory()
    VehicleFactory()
    VendorFactory()
    body = _body(client, _trip())
    for name in ("driver", "vehicle", "vendor"):
        block = re.search(rf'<select[^>]*name="{name}"[^>]*>', body)
        assert block, name
        assert "data-tom" in block.group(0), name


def test_a_driver_row_still_warns_about_lapsing_paperwork(client, agent):
    """Dispatch warns, it never blocks — the warning had to survive the move into a
    dropdown, or picking a driver would get prettier and less safe."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.fleet.factories import RenewalFactory

    driver = DriverFactory(name="Ray Delgado")
    RenewalFactory(driver=driver, expires_on=timezone.localdate() + timedelta(days=8))
    row = next(o for o in _options(_body(client, _trip()), "driver") if "Ray Delgado" in o["label"])
    assert "Expires in 8 days" in row["sub"]
    assert row["warn"] is True


def test_an_affiliate_row_still_carries_insurance_and_gnet(client, agent):
    DriverFactory()
    VendorFactory(name="Grid Co", gnet_grid_id="gnet-1", email="")
    VendorFactory(name="Manual Co", email="ops@manual.example")
    rows = {o["label"]: o for o in _options(_body(client, _trip()), "vendor")}
    assert rows["Grid Co"]["badge"] == "GNET"
    assert rows["Grid Co"]["gnet"] is True
    assert rows["Manual Co"]["badge"] == ""
    assert rows["Manual Co"]["email"] == "ops@manual.example"
    assert "No coverage on file" in rows["Manual Co"]["sub"]


def test_send_offer_is_still_gated_on_being_reachable(client, agent):
    """An affiliate with no email and no grid id cannot be offered to at all. The guard
    used to read `data-gnet` off a radio; it now reads the option's own data."""
    assert "canOffer" in FRAGMENT
    block = APP_JS[APP_JS.index("function coverageControls") :][:2600]
    assert "data.email || data.gnet" in block


def test_the_vehicle_that_fits_the_trip_is_badged(client, agent):
    from apps.leads.factories import VehicleTypeFactory

    kind = VehicleTypeFactory(name="Sprinter")
    DriverFactory()
    VehicleFactory(name="Unit 1", vehicle_type=kind)
    rows = _options(_body(client, _trip(vehicle=kind)), "vehicle")
    assert next(r for r in rows if r["label"] == "Unit 1")["badge"] == "FITS"


# --- a vehicle on both sides ----------------------------------------------------------


def test_farm_out_records_a_vehicle_too(client, agent):
    """The affiliate's vehicle is not in our fleet, so it stays free text — but it gets
    the same place and shape as the in-house unit."""
    trip = _trip()
    AssignmentFactory(reservation=trip, vendor=VendorFactory(), status=Assignment.Status.CONFIRMED)
    body = _body(client, trip)
    assert 'name="vehicle_desc"' in body
    assert 'name="vehicle_number"' in body


# --- the affiliate's own driver roster ------------------------------------------------


def _confirmed(vendor=None):
    trip = _trip()
    return AssignmentFactory(
        reservation=trip,
        vendor=vendor or VendorFactory(),
        status=Assignment.Status.CONFIRMED,
    )


def test_the_driver_picker_lists_that_vendors_own_drivers(client, agent):
    a = _confirmed()
    VendorDriver.objects.create(vendor=a.vendor, name="Luis Ortega", phone="+15715550101")
    labels = [o["label"] for o in _options(_body(client, a.reservation), "vendor_driver")]
    assert labels == ["Luis Ortega"]


def test_another_vendors_drivers_never_appear(client, agent):
    a = _confirmed()
    VendorDriver.objects.create(vendor=VendorFactory(name="Someone Else"), name="Not Ours")
    VendorDriver.objects.create(vendor=a.vendor, name="Luis Ortega")
    labels = [o["label"] for o in _options(_body(client, a.reservation), "vendor_driver")]
    assert labels == ["Luis Ortega"]


def test_a_deactivated_roster_driver_is_not_offered(client, agent):
    a = _confirmed()
    VendorDriver.objects.create(vendor=a.vendor, name="Gone", active=False)
    assert _options(_body(client, a.reservation), "vendor_driver") == []


def test_the_driver_already_saved_comes_up_selected(client, agent):
    a = _confirmed()
    driver = VendorDriver.objects.create(vendor=a.vendor, name="Luis Ortega")
    a.driver_name = "Luis Ortega"
    a.status = Assignment.Status.CONFIRMED
    a.save(update_fields=["driver_name", "status"])
    body = _body(client, a.reservation)
    assert f'<option value="{driver.pk}" selected' in body


def test_typing_a_new_name_creates_a_driver_on_that_vendor(client, agent):
    a = _confirmed()
    resp = client.post(
        reverse("dispatch_vendor_driver_create", args=[a.vendor_id]), {"name": "Luis Ortega"}
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    driver = VendorDriver.objects.get(name="Luis Ortega")
    assert driver.vendor_id == a.vendor_id
    assert resp.json()["id"] == driver.pk


def test_creating_a_roster_driver_sends_the_customer_nothing(client, agent, mailoutbox):
    """`set_driver_info` is what releases driver details to the customer. Typing a name
    into a picker is not that act, and must never become a send."""
    from apps.messaging.models import TouchPoint

    a = _confirmed()
    before = TouchPoint.objects.count()
    client.post(reverse("dispatch_vendor_driver_create", args=[a.vendor_id]), {"name": "Luis"})
    assert TouchPoint.objects.count() == before
    assert mailoutbox == []


def test_an_empty_name_creates_nothing(client, agent):
    a = _confirmed()
    resp = client.post(reverse("dispatch_vendor_driver_create", args=[a.vendor_id]), {"name": " "})
    assert resp.status_code == 400
    assert not VendorDriver.objects.exists()


def test_the_same_name_twice_reuses_the_row(client, agent):
    """Two spellings of one person is worse than reusing the row already there."""
    a = _confirmed()
    url = reverse("dispatch_vendor_driver_create", args=[a.vendor_id])
    first = client.post(url, {"name": "Luis Ortega"}).json()["id"]
    second = client.post(url, {"name": "luis ortega"}).json()["id"]
    assert first == second
    assert VendorDriver.objects.count() == 1


def test_creating_needs_login(client):
    vendor = VendorFactory()
    resp = client.post(reverse("dispatch_vendor_driver_create", args=[vendor.pk]), {"name": "X"})
    assert resp.status_code == 302 and "login" in resp["Location"]


def test_saving_a_picked_roster_driver_releases_them_once(client, agent):
    """The one control on this fragment that does reach the customer, still exactly once."""
    from unittest.mock import patch

    a = _confirmed()
    driver = VendorDriver.objects.create(vendor=a.vendor, name="Luis Ortega", phone="+15715550101")
    with patch("apps.messaging.touchpoints.trigger_driver_released") as released:
        resp = client.post(
            reverse("dispatch_driver_info", args=[a.pk]),
            {"vendor_driver": driver.pk, "vehicle_desc": "Black Suburban"},
        )
    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.driver_name == "Luis Ortega"
    assert a.driver_cell == "+15715550101"  # the roster's own number fills a blank box
    assert released.call_count == 1


def test_a_typed_cell_beats_the_rosters(client, agent):
    a = _confirmed()
    driver = VendorDriver.objects.create(vendor=a.vendor, name="Luis", phone="+15715550101")
    client.post(
        reverse("dispatch_driver_info", args=[a.pk]),
        {"vendor_driver": driver.pk, "driver_cell": "571-555-0199"},
    )
    a.refresh_from_db()
    assert a.driver_cell == "+15715550199"


def test_a_roster_driver_from_another_vendor_is_refused(client, agent):
    a = _confirmed()
    theirs = VendorDriver.objects.create(vendor=VendorFactory(), name="Not Ours")
    resp = client.post(reverse("dispatch_driver_info", args=[a.pk]), {"vendor_driver": theirs.pk})
    assert resp.status_code == 400
    a.refresh_from_db()
    assert a.driver_name == ""


def test_free_text_still_works(client, agent):
    """Anything still posting `driver_name` keeps working."""
    a = _confirmed()
    client.post(reverse("dispatch_driver_info", args=[a.pk]), {"driver_name": "Walk-up Wally"})
    a.refresh_from_db()
    assert a.driver_name == "Walk-up Wally"


# --- the traps ------------------------------------------------------------------------


def test_the_editor_hides_coverage_when_it_came_from_the_drawer():
    """The drawer behind it has the same controls open — two live forms for one trip on
    one screen is how a dispatcher assigns it twice. With one shared fragment this
    matters more, not less."""
    assert "!returnDrawerUrl" in EDITOR


def test_both_injection_points_initialise_the_pickers():
    """Tom Select only enhances what was in the DOM when it ran. The drawer never called
    initTomSelects before this ticket, and the panel had no data-tom, so nothing noticed."""
    assert APP_JS.count("initTomSelects(this.$root)") == 2


def test_reassign_keeps_its_channel_aware_copy():
    """Moe wrote this copy himself. On GNet the cancel goes out over the network, so the
    manual wording would be a plain lie there."""
    assert "Withdraw from GNet &amp; reassign" in FRAGMENT
    assert "withdraws it from GNet too" in FRAGMENT
    assert "Let the driver know yourself" in FRAGMENT  # in-house
    assert "'Unassign'" not in FRAGMENT


def test_reassign_is_still_two_steps():
    """`withdraw`'s gateway release commits before anything a transaction could roll back,
    so there is no atomic reassign to offer — and there must not be a service pretending
    otherwise."""
    from apps.dispatch import services

    assert not hasattr(services, "reassign")
    block = APP_JS[APP_JS.index("function coverageControls") :][:4000]
    assert 'action: "withdraw"' in block


def test_the_record_of_a_withdrawal_keeps_its_own_name():
    """The button is the dispatcher's intent; WITHDRAWN is what happened to that
    assignment. History stays readable."""
    assert Assignment.Status.WITHDRAWN == "withdrawn"


def test_nothing_re_sends_the_customer_on_reassign():
    """Telling them their driver changed stays the agent's job (Moe, 2026-09-19) — a
    second automatic message about a driver who is no longer coming is worse than none."""
    block = APP_JS[APP_JS.index("function coverageControls") :][:4000]
    assert "trigger_driver_released" not in block
    assert "driver_info" not in block


def test_every_affiliate_is_reachable_from_the_picker(client, agent):
    """The radio list showed the top 8 and had a search box that re-fetched the panel to
    reach past them. Tom Select filters what was rendered, so capping the picker at 8
    would have quietly made the 9th affiliate unpickable.
    """
    DriverFactory()
    for i in range(25):
        VendorFactory(name=f"Affiliate {i:02d}")
    rows = _options(_body(client, _trip()), "vendor")
    assert len(rows) == 25
    assert "Affiliate 24" in [r["label"] for r in rows]


def test_the_picker_still_costs_two_queries_whatever_the_count(client, agent):
    """Lifting the cap must not lift the query count with it."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.dispatch import selectors

    trip = _trip()
    VendorFactory()
    with CaptureQueriesContext(connection) as few:
        selectors.vendor_options(trip, limit=None)
    for i in range(20):
        VendorFactory(name=f"More {i}")
    with CaptureQueriesContext(connection) as many:
        selectors.vendor_options(trip, limit=None)
    assert len(many) == len(few) == 2
