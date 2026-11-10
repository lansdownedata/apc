"""APC-102 — the Trip Processing screen and its endpoints."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview

pytestmark = pytest.mark.django_db
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        yield


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def accountant(client):
    user = UserFactory(can_manage_payments=True)
    client.force_login(user)
    client.user = user
    return client


def _trip(status=S.DONE, *, contact=None, lead=None, days_ago=1, **kw):
    lead = lead or LeadFactory(status=Lead.Status.BOOKED, contact=contact or ContactFactory())
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_time=time(10, 0),
        pickup_timezone="America/Los_Angeles",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _paid(trip):
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=trip.line_total)
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=trip.line_total,
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_full",
        idempotency_key=f"bal-{trip.pk}",
    )
    ledger.post_capture(
        lead=trip.lead,
        amount=trip.line_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"paid-{trip.pk}",
    )
    return plan


def _page(client, **query):
    return client.get(reverse("processing"), query)


def test_the_screen_needs_a_login(client):
    assert _page(client).status_code == 302


def test_the_list_groups_trips_by_customer(staff):
    trip = _trip(contact=ContactFactory(name="Priya Okafor"))
    _trip(lead=trip.lead, days_ago=2)

    body = _page(staff).content.decode()

    assert "Priya Okafor" in body and trip.reference in body
    assert "Ready to process" in body and "<dialog" not in body


def test_selecting_a_trip_renders_its_pane_with_price_lines_and_edit_price(staff):
    trip = _trip(gratuity_pct=Decimal("20"))
    _paid(trip)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "data-price-lines" in body and "Gratuity (20%)" in body
    assert "Edit price" in body and "reservation-edit" in body
    assert "trip-review-close" not in body  # the pop-up's close button is gone here
    assert "PDT" in body or "PST" in body  # the trip's own zone


def test_a_deep_link_opens_its_trip_whatever_the_filters(staff):
    trip = _trip(days_ago=200)
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    body = _page(staff, trip=trip.pk, view="ready").content.decode()

    assert "data-price-lines" in body and "Processed" in body


def test_the_footer_offers_the_right_way_forward(accountant):
    trip = _trip()
    _paid(trip)
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "processingPane(" in body and "Process and send pay link" in body
    assert "To charge now" in body and "200.00" in body


def test_a_trip_without_a_status_offers_the_status_buttons(accountant):
    trip = _trip(S.CUSTOMER_IN_CAR)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "No final status arrived" in body and "No Show" in body


def test_without_payments_access_the_footer_only_reads(staff):
    trip = _trip()
    _paid(trip)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "Processing needs payments access" in body and "processNone()" not in body


def test_the_processed_pane_shows_the_banner_history_and_unlock(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "Times and charges are locked" in body and "History" in body
    assert "Unlock to edit" in body and "Add a charge" in body


@pytest.mark.parametrize(
    "name",
    ["processing_process", "processing_unlock", "processing_add_charge", "processing_refund"],
)
def test_the_actions_need_payments_access(staff, name):
    trip = _trip()
    assert staff.post(reverse(name, args=[trip.pk])).status_code == 403


def test_process_endpoint_locks_and_reports_errors_as_json(accountant):
    trip = _trip()
    _paid(trip)

    bad = accountant.post(reverse("processing_process", args=[trip.pk]), {"expected": "9.99"})
    ok = accountant.post(reverse("processing_process", args=[trip.pk]), {"expected": "0.00"})

    assert bad.status_code == 400 and "price changed" in bad.json()["error"]
    assert ok.json()["ok"] is True
    assert TripReview.objects.get(reservation=trip).is_processed


def test_unlock_endpoint_needs_a_reason(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    resp = accountant.post(reverse("processing_unlock", args=[trip.pk]), {"reason": ""})

    assert resp.status_code == 400 and "why" in resp.json()["error"]


def test_set_status_marks_what_happened(staff):
    trip = _trip(S.CUSTOMER_IN_CAR)

    resp = staff.post(reverse("processing_set_status", args=[trip.pk]), {"status": "no_show"})

    trip.refresh_from_db()
    assert resp.json()["ok"] is True and trip.trip_status == S.NO_SHOW


def test_set_status_refuses_anything_but_the_three(staff):
    trip = _trip(S.CUSTOMER_IN_CAR)
    resp = staff.post(reverse("processing_set_status", args=[trip.pk]), {"status": "cancelled"})
    assert resp.status_code == 400


def test_the_nav_shows_processing(staff):
    body = staff.get(reverse("dashboard")).content.decode()
    assert reverse("processing") in body and ">Processing<" in body


def test_the_list_costs_a_flat_number_of_queries(staff):
    _trip()
    _page(staff)
    with CaptureQueriesContext(connection) as one:
        _page(staff)
    for _ in range(30):
        _trip(contact=ContactFactory())
    with CaptureQueriesContext(connection) as many:
        _page(staff)

    assert len(many) == len(one)


# --- branch-review fixes -------------------------------------------------------------------


def test_a_processed_trip_shows_no_moved_balance_banner(accountant):
    """Moe 2026-10-25: a processed trip was charged cleanly; no banner."""
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")
    Reservation.objects.filter(pk=trip.pk).update(overtime_hours=Decimal("1"))

    body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "has moved since it was processed" not in body


def test_the_issues_drill_down_lists_every_trip_with_an_open_issue(staff):
    from apps.reservations.models import TripIssue

    open_one = _trip(contact=ContactFactory(name="Flagged Customer"))
    _trip(contact=ContactFactory(name="Quiet Customer"))
    review = TripReview.objects.create(reservation=open_one, processed_at=timezone.now())
    TripIssue.objects.create(review=review, category="driver", severity="low", note="Late")

    body = _page(staff, issues="1").content.decode()

    assert "Flagged Customer" in body and "Quiet Customer" not in body
    from apps.tasks import exceptions

    assert exceptions.counts()["open_issues"] == 1


def test_a_deep_link_to_a_trip_with_nothing_to_process_says_why(staff):
    trip = _trip(S.CANCELLED)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "has nothing to process" in body


def test_the_old_order_url_opens_its_first_workable_trip(staff):
    cancelled = _trip(S.CANCELLED, days_ago=3)
    done = _trip(lead=cancelled.lead, days_ago=2)

    resp = staff.get(reverse("trip_review_order", args=[cancelled.lead_id]))

    assert resp.url == f"{reverse('processing')}?trip={done.pk}"


def test_processed_refund_is_refused_on_an_order_that_is_not_booked(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")
    Lead.objects.filter(pk=trip.lead_id).update(status=Lead.Status.LOST)

    resp = accountant.post(
        reverse("processing_refund", args=[trip.pk]), {"amount": "50", "reason": "x"}
    )

    assert resp.status_code == 400


def test_a_bad_refund_amount_is_a_message_not_a_500(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    resp = accountant.post(
        reverse("processing_refund", args=[trip.pk]), {"amount": "NaN", "reason": "x"}
    )

    assert resp.status_code == 400 and "valid amount" in resp.json()["error"]


def test_save_errors_have_a_place_to_show_in_processing(accountant):
    trip = _trip()
    _paid(trip)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert 'x-text="error"' in body.split("processingPane(")[1]


def test_the_start_date_covers_post_trip_work_already_open():
    import importlib

    from django.apps import apps as django_apps

    from apps.tasks.models import Task, TaskConfig

    module = importlib.import_module("apps.tasks.migrations.0012_processing_from")
    old = _trip(days_ago=20)
    Task.objects.create(lead=old.lead, reservation=old, kind="ops_review", opens_at=timezone.now())
    TaskConfig.load()
    TaskConfig.objects.update(processing_from=None)

    module.start_today(django_apps, None)

    assert TaskConfig.load().processing_from == old.pickup_date


# --- Moe's answers 2026-10-25: sortable list, remembered per user; no moved-balance banner --


def _names(resp) -> list[str]:
    return [g.contact.name for g in resp.context["groups"]]


def test_ready_defaults_to_the_oldest_trip_first(staff):
    _trip(contact=ContactFactory(name="Zed Newer"), days_ago=1)
    _trip(contact=ContactFactory(name="Amy Older"), days_ago=5)

    assert _names(_page(staff)) == ["Amy Older", "Zed Newer"]


def test_a_chosen_sort_is_remembered_for_that_user(client):
    user = UserFactory()
    client.force_login(user)
    _trip(contact=ContactFactory(name="Zed Older"), days_ago=5)
    _trip(contact=ContactFactory(name="Amy Newer"), days_ago=1)

    assert _names(_page(client, sort="customer")) == ["Amy Newer", "Zed Older"]
    assert _names(_page(client)) == ["Amy Newer", "Zed Older"]  # remembered
    assert _names(_page(client, sort="newest")) == ["Amy Newer", "Zed Older"]
    user.refresh_from_db()
    assert user.preferences["processing"]["ready_sort"] == "newest"


def test_another_users_sort_is_their_own(client):
    other = UserFactory(preferences={"processing": {"ready_sort": "customer"}})
    assert other.preferences
    client.force_login(UserFactory())
    _trip(contact=ContactFactory(name="Zed Older"), days_ago=5)
    _trip(contact=ContactFactory(name="Amy Newer"), days_ago=1)

    assert _names(_page(client)) == ["Zed Older", "Amy Newer"]


def test_an_unknown_sort_falls_back_to_the_default(staff):
    _trip(contact=ContactFactory(name="Zed Newer"), days_ago=1)
    _trip(contact=ContactFactory(name="Amy Older"), days_ago=5)

    assert _names(_page(staff, sort="'; drop")) == ["Amy Older", "Zed Newer"]


def test_processed_sorts_too(accountant):
    old = _trip(contact=ContactFactory(name="Old Pickup"), days_ago=6)
    new = _trip(contact=ContactFactory(name="New Pickup"), days_ago=2)
    for t in (new, old):  # old processed last = most recently processed
        _paid(t)
        processing.process(t, user=accountant.user, expected="0.00")

    default = [t.pk for t in _page(accountant, view="processed").context["trips"]]
    oldest = [t.pk for t in _page(accountant, view="processed", sort="oldest").context["trips"]]

    assert default == [old.pk, new.pk]
    assert oldest == [old.pk, new.pk]
    newest = [t.pk for t in _page(accountant, view="processed", sort="newest").context["trips"]]
    assert newest == [new.pk, old.pk]


def test_the_list_offers_the_sort_picker(staff):
    body = _page(staff).content.decode()

    assert 'name="sort"' in body


def test_an_off_card_refund_from_the_screen_shows_how_it_went_back(accountant):
    trip = _trip()
    _paid(trip)

    resp = accountant.post(
        reverse("processing_refund", args=[trip.pk]),
        {"amount": "40", "reason": "Goodwill", "method": "check", "reference": "1042"},
    )

    assert resp.status_code == 200, resp.content  # Ready trips can be refunded too
    body = _page(accountant, trip=trip.pk).content.decode()
    assert "Check 1042" in body


def test_the_refund_modal_knows_what_the_card_can_still_take(accountant):
    trip = _trip()
    _paid(trip)

    footer = _page(accountant, trip=trip.pk).content.decode().split("processingPane(")[1]

    assert "cardRefundable: &#x27;600.00&#x27;" in footer or "cardRefundable: '600.00'" in footer
    assert "Hold as credit" not in footer  # nothing overpaid here


def _processed_pane(accountant, **charges):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")
    made = {name: ChargeFactory(reservation=trip, **fields) for name, fields in charges.items()}
    return trip, made


def test_processed_pane_shows_receipt_state_per_charge(accountant):
    trip, made = _processed_pane(
        accountant,
        synced={
            "kind": Charge.Kind.TRIP,
            "status": Charge.Status.SUCCEEDED,
            "qbo_sync_state": "synced",
        },
        failed={
            "kind": Charge.Kind.TRIP,
            "status": Charge.Status.SUCCEEDED,
            "qbo_sync_state": "error",
            "qbo_sync_error": "Customer is inactive.",
        },
        deposit={"kind": Charge.Kind.DEPOSIT, "status": Charge.Status.SUCCEEDED},
        changed={
            "kind": Charge.Kind.TRIP,
            "status": Charge.Status.SUCCEEDED,
            "qbo_id": "900",
            "qbo_sync_state": "not_synced",
        },
        declined={
            "kind": Charge.Kind.TRIP,
            "status": Charge.Status.FAILED,
            "qbo_sync_state": "error",
            "qbo_sync_error": "Declined row sentence.",
        },
    )

    with patch("apps.integrations.qbo.is_connected", return_value=True):
        body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "In QuickBooks" in body
    assert "Customer is inactive." in body
    assert reverse("charge_qbo_sync", args=[made["failed"].pk]) in body
    assert reverse("charge_qbo_sync", args=[made["synced"].pk]) not in body
    assert reverse("charge_qbo_sync", args=[made["deposit"].pk]) not in body
    assert body.count("Not sent yet") == 0
    assert body.count("Changed since it was sent") == 1
    assert reverse("charge_qbo_sync", args=[made["changed"].pk]) in body
    assert "Declined row sentence." not in body
    assert reverse("charge_qbo_sync", args=[made["declined"].pk]) not in body


def test_processed_pane_quiet_when_disconnected(accountant):
    trip, made = _processed_pane(
        accountant,
        failed={
            "kind": Charge.Kind.TRIP,
            "status": Charge.Status.SUCCEEDED,
            "qbo_sync_state": "error",
            "qbo_sync_error": "Customer is inactive.",
        },
    )

    with patch("apps.integrations.qbo.is_connected", return_value=False):
        body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "Customer is inactive." not in body
    assert "In QuickBooks" not in body and "Not sent yet" not in body
    assert reverse("charge_qbo_sync", args=[made["failed"].pk]) not in body


def test_processed_pane_query_count_is_flat_with_receipt_state(accountant):
    from datetime import date

    from apps.billing.models import QboAccountMapping

    mapping = QboAccountMapping.load()
    mapping.customer_docs_from = date(2020, 1, 1)
    mapping.save()
    trip, _ = _processed_pane(accountant)

    def add(n):
        for _ in range(n):
            ChargeFactory(reservation=trip, kind=Charge.Kind.TRIP, status=Charge.Status.SUCCEEDED)

    def count():
        with patch("apps.integrations.qbo.is_connected", return_value=True):
            with CaptureQueriesContext(connection) as ctx:
                _page(accountant, trip=trip.pk, view="processed")
        return len(ctx)

    add(1)
    count()  # warm the per-process caches
    one = count()
    add(4)
    assert count() == one


# --- PROC-02: the overtime gate and Waive, in the browser ------------------------------


def _undecided(trip, minutes=60):
    from apps.reservations import reviews

    review = reviews.review_for(trip)
    TripReview.objects.filter(pk=review.pk).update(
        suggested_overtime_minutes=minutes, decided_at=None
    )


def test_the_footer_knows_the_overtime_is_undecided(accountant):
    trip = _trip()
    _paid(trip)
    _undecided(trip)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "overtimeUndecided: true" in body


def test_the_footer_knows_when_there_is_nothing_to_decide(accountant):
    trip = _trip()
    _paid(trip)
    _undecided(trip, minutes=0)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "overtimeUndecided: false" in body


def _node(harness: str, source: str) -> list:
    import json
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        ["node", "-e", harness], input=source, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _js(name: str) -> str:
    from pathlib import Path

    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index(f"function {name}(")
    return js[start : js.index(f"window.{name} = {name};", start)]


_GATE_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const out = [];
const ctx = {
  out,
  document: { getElementById: () => null, querySelector: () => null },
  Alpine: { store: (n) => (n === "modal"
    ? { confirm: (o) => out.push(["modal", o.title]), show: (o) => out.push(["modal", o.title]) }
    : { push: (t) => out.push(["toast", t.title]) }) },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(0, "utf8") + `
  const base = { difference: "100.00", card: "Visa ••4242", urls: { process: "/p/" } };
  const p = processingPane({ ...base, overtimeUndecided: true });
  p.processCard(); p.processLink(); p.processNone(); p.processInvoice();
  p.processRefund(); p.processKeep();
  const q = processingPane({ ...base, overtimeUndecided: false });
  q.processCard();
`, ctx);
process.stdout.write(JSON.stringify(out));
"""


def test_process_is_refused_in_the_browser_until_the_overtime_is_decided():
    """PROC-02: the charge confirm opened first, and only its Confirm met the server's
    refusal."""
    out = _node(_GATE_HARNESS, _js("processingPane"))
    gate = ["toast", "Decide the overtime first: keep or change the suggestion."]
    assert out[:6] == [gate] * 6
    assert out[6] == ["modal", "Charge $100.00?"]


_WAIVE_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const saved = [];
const ctx = { saved };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(0, "utf8") + `
  const f = tripReviewForm({
    state: { billable_minutes: 0, waived: true, suggested_minutes: 60, decided: true },
    waiveReason: "traffic",
  });
  f._save = (data) => saved.push(data);
  f.waived = false;   // the agent unticks Waive
  f.saveDecision();
`, ctx);
process.stdout.write(JSON.stringify(saved));
"""


_SPLIT_HARNESS = """
const vm = require("vm");
const fs = require("fs");
const out = [];
const ctx = {
  out,
  Alpine: { store: (n) => (n === "modal"
    ? { confirm: (o) => out.push([o.title, o.message, o.confirmText]) }
    : { push() {} }) },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(0, "utf8") + `
  const base = { difference: "650.00", card: "Visa ••4242", urls: { process: "/p/" } };
  processingPane({ ...base, fromHold: "500.00", fromCard: "150.00" }).processCard();
  const all = { ...base, difference: "250.00", fromHold: "250.00", fromCard: "0.00" };
  processingPane(all).processCard();
  processingPane({ ...base, fromHold: "0.00", fromCard: "650.00" }).processCard();
`, ctx);
process.stdout.write(JSON.stringify(out));
"""


def test_the_charge_confirm_says_what_the_hold_pays():
    """HOLD-07 / HOLD-12: the confirm said "charged now to Visa ••4242" for money the
    cleaning hold paid."""
    split, all_hold, card_only = _node(_SPLIT_HARNESS, _js("processingPane"))
    assert split == [
        "Collect $650.00?",
        "$500.00 from the cleaning hold, $150.00 charged now to Visa ••4242. "
        "Then the trip is locked.",
        "Collect $650.00",
    ]
    assert all_hold == [
        "Collect $250.00?",
        "All of it from the cleaning hold; the card isn't charged again. Then the trip is locked.",
        "Collect $250.00",
    ]
    assert card_only == [
        "Charge $650.00?",
        "What the trip still owes, charged now to Visa ••4242. Then the trip is locked.",
        "Charge $650.00",
    ]


def test_unticking_waive_goes_back_to_the_suggestion():
    """PROC-02: unticking Waive saved 0 billable minutes, the value the waive had left."""
    (saved,) = _node(_WAIVE_HARNESS, _js("tripReviewForm"))
    assert saved["billable_minutes"] == 60
    assert saved["waived"] == "0"
