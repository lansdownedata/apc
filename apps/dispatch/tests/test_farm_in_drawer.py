"""APC-136 — the GNet requests drawer: tabs, rows, pills, the header icon and its cost."""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.template import Context, Template
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.dispatch import farm_in, farm_in_drawer
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db

URL = "/portal/farm-in/drawer/"
S = FarmInRequest.Status


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(
        rate=Decimal("165"), transfer_min_hours=1, hourly_min_hours=4
    )
    vt.refresh_from_db()
    return vt


_seq = iter(range(10_000))


def _arrive(payload: dict | None = None, **fields) -> FarmInRequest:
    """A pending request as the receiver builds it, then any envelope fields overwritten."""
    tid = f"tx-drawer-{next(_seq)}"
    farm_in.handle_new(payload or sample_payload(), transaction_id=tid, delivery_id="d")
    if fields:
        FarmInRequest.objects.filter(transaction_id=tid).update(**fields)
    return FarmInRequest.objects.get(transaction_id=tid)


def _fragment(client, **params) -> str:
    resp = client.get(URL, params)
    assert resp.status_code == 200
    return resp.content.decode()


def _row_pks(body: str) -> list[int]:
    return [int(pk) for pk in re.findall(r'data-fi="(\d+)"', body)]


def test_fragment_url_is_named():
    assert reverse("farm_in_drawer") == URL


def test_fragment_requires_login(client):
    resp = client.get(URL)
    assert resp.status_code == 302
    assert "login" in resp.url


def test_pending_tab_lists_pending_sorted_nulls_first(logged_in_client, sedan):
    late = _arrive(pickup_at=datetime(2026, 11, 9, 23, 0, tzinfo=UTC))
    unknown = _arrive(pickup_at=None)
    soon = _arrive(pickup_at=datetime(2026, 11, 8, 18, 27, tzinfo=UTC))
    FarmInRequestFactory(status=S.ACCEPTED)

    rows = farm_in_drawer.rows(tab="pending", dispatch_only=False)

    assert [r.pk for r in rows] == [unknown.pk, soon.pk, late.pk]
    assert _row_pks(_fragment(logged_in_client)) == [unknown.pk, soon.pk, late.pk]


def test_dispatch_only_narrows_to_dispatch_desk(logged_in_client, sedan):
    _arrive()
    at_dispatch = _arrive(desk=FarmInRequest.Desk.DISPATCH)

    rows = farm_in_drawer.rows(tab="pending", dispatch_only=True)
    body = _fragment(logged_in_client, tab="pending", dispatch="1")

    assert [r.pk for r in rows] == [at_dispatch.pk]
    assert _row_pks(body) == [at_dispatch.pk]
    assert reverse("farm_in_send_back", args=[at_dispatch.pk]) in body
    assert reverse("farm_in_request_coverage", args=[at_dispatch.pk]) not in body


def test_reservations_desk_row_offers_coverage_and_the_workspace(logged_in_client, sedan):
    req = _arrive()

    body = _fragment(logged_in_client)

    assert 'data-action="accept"' in body
    assert reverse("farm_in_accept", args=[req.pk]) in body
    assert reverse("farm_in_request_coverage", args=[req.pk]) in body
    assert reverse("farm_in_decline", args=[req.pk]) in body
    assert reverse("farm_in_send_back", args=[req.pk]) not in body
    assert reverse("lead_detail", args=[req.lead_id]) in body
    assert "Open workspace" in body
    received = req.received_at.astimezone(UTC).isoformat(timespec="seconds")
    assert f'data-received="{received}"' in body
    assert "their res 1715016686" in body
    assert req.company.name in body


def test_accepted_tab_shows_order_link_and_pushes(logged_in_client):
    req = FarmInRequestFactory(status=S.ACCEPTED)
    FarmInStatusPushFactory(
        request=req,
        status="CONFIRMED",
        total_amount=Decimal("495.00"),
        state=FarmInStatusPush.State.SENT,
        attempts=1,
        sent_at=datetime(2026, 11, 7, 15, 0, tzinfo=UTC),
    )
    failed = FarmInStatusPushFactory(
        request=req, status="DRIVER_ASSIGNED", state=FarmInStatusPush.State.FAILED, attempts=8
    )
    fast = FarmInStatusPushFactory(
        request=req,
        status="REJECT",
        state=FarmInStatusPush.State.FAILED,
        attempts=0,
        last_error="GNet key missing " + "x" * 80,
    )
    once = FarmInStatusPushFactory(
        request=req, status="CANCELED", state=FarmInStatusPush.State.FAILED, attempts=1
    )
    _pending = FarmInStatusPushFactory(request=FarmInRequestFactory(), status="PENDING")

    body = _fragment(logged_in_client, tab="accepted")

    assert _row_pks(body) == [req.pk]
    assert reverse("order_detail", args=[req.lead_id]) in body
    assert f"Order {req.lead.quote_no}" in body
    assert "Reported to GNet" in body
    assert "CONFIRMED" in body and "$495.00" in body
    assert "DRIVER_ASSIGNED" in body and "failed after 8 tries" in body
    assert reverse("farm_in_retry_push", args=[failed.pk]) in body
    assert "failed: GNet key missing" in body and "failed after 0" not in body
    assert "failed after 1 try<" in body
    assert reverse("farm_in_retry_push", args=[fast.pk]) in body
    assert reverse("farm_in_retry_push", args=[once.pk]) in body
    assert "PENDING" not in body


def test_closed_tab_shows_reason(logged_in_client):
    declined = FarmInRequestFactory(status=S.DECLINED, decline_reason="No sedans that night")
    canceled = FarmInRequestFactory(status=S.CANCELED)
    expired = FarmInRequestFactory(status=S.EXPIRED)
    FarmInRequestFactory()  # pending: not closed
    FarmInStatusPushFactory(request=declined, status="REJECT", state=FarmInStatusPush.State.SENT)

    body = _fragment(logged_in_client, tab="closed")

    assert sorted(_row_pks(body)) == sorted([declined.pk, canceled.pk, expired.pk])
    assert "No sedans that night" in body
    assert "Requester canceled" in body
    assert "Expired" in body
    assert "REJECT" in body
    assert "Nothing sent to GNet" in body


@pytest.mark.parametrize(
    ("fields", "unmapped", "expected"),
    [
        ({"company_created": False}, False, []),
        ({"company_created": False, "desk": "dispatch"}, False, ["Pending dispatch"]),
        ({"company_created": True}, False, ["New affiliate"]),
        ({"company_created": False}, True, ["Unmapped vehicle"]),
        (
            {"company_created": False, "alert": f"Something else · {farm_in.ALERT_UPDATED}"},
            False,
            ["Updated by requester"],
        ),
        (
            {"company_created": True, "desk": "dispatch", "alert": farm_in.ALERT_UPDATED},
            True,
            ["Pending dispatch", "New affiliate"],
        ),
    ],
    ids=["plain", "dispatch", "new-affiliate", "unmapped", "updated", "at-most-two"],
)
def test_row_pills(logged_in_client, sedan, fields, unmapped, expected):
    payload = sample_payload()
    if unmapped:
        payload["preferredVehicleType"] = "BLIMP"
    _arrive(payload, **fields)

    (row,) = farm_in_drawer.rows(tab="pending", dispatch_only=False)
    body = _fragment(logged_in_client)

    assert [label for label, _css in row.pills] == expected
    for label in expected:
        assert label in body
    for label in {"Pending dispatch", "New affiliate", "Unmapped vehicle", "Updated by requester"}:
        if label not in expected:
            assert f">{label}<" not in body


def test_price_slot_says_price_it_without_vehicle(logged_in_client, sedan):
    payload = sample_payload()
    payload["preferredVehicleType"] = "BLIMP"
    _arrive(payload)

    body = _fragment(logged_in_client)

    assert "Price it" in body
    assert 'title="Price the trip first"' in body


def test_price_slot_shows_the_trip_total_when_priced(logged_in_client, sedan):
    _arrive()

    body = _fragment(logged_in_client)

    assert "$165.00" in body
    assert "Price it" not in body
    assert 'title="Price the trip first"' not in body
    assert 'data-price="$165.00"' in body


def test_route_uses_airport_code_and_flight(sedan):
    _arrive()

    (row,) = farm_in_drawer.rows(tab="pending", dispatch_only=False)

    assert row.route == "Beverly Hills → LAX, UA 123"
    assert row.kind == "airport"


def test_hourly_route_text(sedan):
    payload = sample_payload()
    payload["runType"] = "Hourly"
    _arrive(payload)

    (row,) = farm_in_drawer.rows(tab="pending", dispatch_only=False)

    assert row.route == "Beverly Hills, 4 h as directed"
    assert row.kind == "hourly"


def test_counts_in_tab_labels(logged_in_client, django_assert_num_queries):
    FarmInRequestFactory()
    FarmInRequestFactory(desk=FarmInRequest.Desk.DISPATCH)
    FarmInRequestFactory(status=S.ACCEPTED)
    for status in (S.DECLINED, S.CANCELED, S.EXPIRED):
        FarmInRequestFactory(status=status)

    with django_assert_num_queries(1):
        counts = farm_in_drawer.counts()
    body = _fragment(logged_in_client)

    assert counts == {"pending": 2, "accepted": 1, "closed": 3, "dispatch": 1}
    assert farm_in_drawer.pending_count() == 2
    assert "Pending · 2" in body
    assert "Accepted · 1" in body
    assert "Closed · 3" in body
    assert 'data-pending="2"' in body


def test_empty_state(logged_in_client):
    assert "Nothing waiting from GNet." in _fragment(logged_in_client)
    assert "Nothing accepted yet." in _fragment(logged_in_client, tab="accepted")
    assert "Nothing closed yet." in _fragment(logged_in_client, tab="closed")


def test_unknown_tab_falls_back_to_pending(logged_in_client):
    body = _fragment(logged_in_client, tab="bogus")

    assert "Nothing waiting from GNet." in body


def _cost(client, **params) -> int:
    with CaptureQueriesContext(connection) as ctx:
        _fragment(client, **params)
    return len(ctx)


@pytest.mark.parametrize("tab", ["pending", "accepted", "closed"])
def test_fragment_query_count_is_flat(logged_in_client, sedan, tab):
    status = {"pending": S.PENDING, "accepted": S.ACCEPTED, "closed": S.DECLINED}[tab]

    def add(n: int) -> None:
        for _ in range(n):
            req = _arrive(status=status)
            FarmInStatusPushFactory(request=req, state=FarmInStatusPush.State.SENT)

    add(3)
    few = _cost(logged_in_client, tab=tab)
    add(5)
    many = _cost(logged_in_client, tab=tab)

    assert many == few


def _dashboard(client) -> str:
    return client.get(reverse("dashboard")).content.decode()


def test_header_icon_shows_count_and_ring(logged_in_client):
    quiet = _dashboard(logged_in_client)
    assert "ti-arrows-exchange-2" in quiet
    assert 'x-data="{n: 0}"' in quiet
    assert not re.search(r'\sclass="[^"]*ring-gold/40', quiet)

    FarmInRequestFactory()
    FarmInRequestFactory()
    busy = _dashboard(logged_in_client)

    assert 'x-data="{n: 2}"' in busy
    assert re.search(r'\sclass="[^"]*ring-gold/40', busy)
    assert re.search(r"data-fi-badge.*?>2</span>", busy, re.DOTALL)
    assert 'x-data="farmInDrawer()"' in busy


def test_chrome_adds_one_query_at_most(logged_in_client):
    # Only the farm-in queries: each request also brings a lead, which the dashboard's
    # recent-leads list pays for on its own (not what this measures).
    def farm_in_queries() -> int:
        with CaptureQueriesContext(connection) as ctx:
            logged_in_client.get(reverse("dashboard"))
        return sum("dispatch_farminrequest" in q["sql"] for q in ctx.captured_queries)

    # The tray and the dashboard's alerts join their request (a mention each);
    # the count is one more. The cost stays flat as requests arrive.
    before = farm_in_queries()
    assert before == 3
    for _ in range(3):
        n = FarmInRequestFactory()
        Notification.notify(n.lead, Notification.Kind.FARM_IN_NEW, title="t", farm_in_request=n)
    assert farm_in_queries() == before


def _clock(dt, tz_name) -> str:
    tpl = Template("{% load time_filters %}{{ dt|zone_clock:tz }}")
    return tpl.render(Context({"dt": dt, "tz": tz_name}))


def test_zone_clock(settings):
    settings.TIME_ZONE = "America/New_York"
    dt = datetime(2026, 11, 8, 18, 27, tzinfo=UTC)

    assert _clock(dt, "America/Los_Angeles") == "Sun Nov 8 · 10:27 AM PST"
    assert _clock(dt, "America/New_York") == "Sun Nov 8 · 1:27 PM"
    assert _clock(dt, "") == "Sun Nov 8 · 1:27 PM"
    assert _clock(datetime(2026, 7, 4, 1, 0, tzinfo=UTC), "America/Los_Angeles") == (
        "Fri Jul 3 · 6:00 PM PDT"
    )
    assert _clock(None, "America/Los_Angeles") == ""


def test_banner_and_drawer_share_a_helper_that_is_not_shadowed():
    """`app.js` is a classic script: a top-level `function farmInAct` IS `window.farmInAct`,
    so assigning the banner's arrow to that name made every Accept/Decline call itself."""
    from pathlib import Path

    from django.conf import settings

    js = (Path(settings.BASE_DIR) / "static" / "js" / "app.js").read_text()
    assert "function farmInConfirm(" in js
    assert "function farmInAct(" not in js
    window_line = next(ln for ln in js.splitlines() if ln.startswith("window.farmInAct ="))
    following = js[js.index(window_line) :][:200]
    assert "farmInConfirm(" in following
    assert "farmInAct(kind" not in following


def test_pending_row_on_a_lost_lead_says_so_and_cannot_be_accepted(logged_in_client, sedan):
    from apps.leads.models import Lead

    req = _arrive(company_created=True, desk="dispatch")
    Lead.objects.filter(pk=req.lead_id).update(status=Lead.Status.LOST)

    (row,) = farm_in_drawer.rows(tab="pending", dispatch_only=False)
    body = _fragment(logged_in_client)

    assert [label for label, _css in row.pills][0] == "Lead lost"
    assert "Lead lost" in body
    assert 'disabled title="Lead is lost"' in body
