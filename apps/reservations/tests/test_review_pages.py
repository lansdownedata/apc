"""APC-59 — the Trip Review pages and the trip review endpoints."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue, TripReview
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _order(*, farmed_out=True, status=Reservation.TripStatus.DONE, zone="America/Los_Angeles"):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(22, 0),
        pickup_timezone=zone,
        rate=Decimal("168"),
        hours=Decimal("3"),
        trip_type=Reservation.TripType.HOURLY,
        trip_status=status,
    )
    if farmed_out:
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, payout=420)
    else:
        AssignmentFactory(reservation=trip, in_house=True)
    PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    run_tasks()
    return lead, trip


def _trip_url(name, trip):
    return reverse(name, args=[trip.pk])


# --- list ----------------------------------------------------------------------------


def test_the_list_needs_a_login(client):
    assert client.get(reverse("trip_review_list")).status_code == 302


def test_the_list_shows_orders_in_review(staff):
    lead, _trip = _order()

    body = staff.get(reverse("trip_review_list")).content.decode()

    assert lead.quote_no in body
    assert "0 of 1 reviewed" in body
    assert "Needs ops review" in body
    assert "Total due" in body and "Remaining" in body
    assert "data-tom" in body  # filters are Tom Select, never a bare <select>
    assert "<dialog" not in body


def test_the_sidebar_has_a_trip_review_entry(staff):
    body = staff.get(reverse("dashboard")).content.decode()

    assert reverse("trip_review_list") in body
    assert "Trip Review" in body


def test_the_list_page_costs_the_same_for_two_orders_or_eight(staff):
    for _ in range(2):
        _order()
    with CaptureQueriesContext(connection) as few:
        staff.get(reverse("trip_review_list"))
    for _ in range(6):
        _order()
    with CaptureQueriesContext(connection) as many:
        staff.get(reverse("trip_review_list"))

    assert len(many) == len(few)


# --- order ---------------------------------------------------------------------------


def test_the_order_page(staff):
    lead, trip = _order()
    cancelled = ReservationFactory(
        lead=lead,
        pickup_date=trip.pickup_date,
        pickup_time=time(9, 0),
        pickup_timezone="America/Los_Angeles",
        trip_status=Reservation.TripStatus.CANCELLED,
    )

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert lead.quote_no in body
    assert "Trips reviewed" in body and "0 / 1" in body
    assert "Needs review" in body
    assert "Cancelled · nothing to review" in body
    assert "Money" in body and "Remaining to collect" in body
    assert _trip_url("trip_review_trip", trip) in body
    assert _trip_url("trip_review_trip", cancelled) not in body


def test_a_trip_with_no_status_says_it_ended_by_schedule(staff):
    lead, _trip = _order(status="")

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "No status · ended by schedule" in body


def test_a_reviewed_trip_shows_its_billed_overtime(staff):
    lead, trip = _order()
    start = trip.pickup_at
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=30),
        billable_overtime_minutes=30,
    )
    reviews.complete_review(review, user=UserFactory())

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Reviewed · 30 min OT billed ($168.00)" in body  # 30 min bills the hour


def test_an_order_not_in_review_404s(staff):
    lead = LeadFactory(status=Lead.Status.BOOKED)

    assert staff.get(reverse("trip_review_order", args=[lead.pk])).status_code == 404


# --- the trip review pop-up ----------------------------------------------------------


def test_the_trip_review_shows_both_panes_in_the_trips_zone(staff):
    _lead, trip = _order()

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Customer billing" in body and "Driver pay" in body
    assert "Affiliate overtime" in body and "Expected to pay" in body
    assert "Approve payable" not in body  # a plain agent can't approve
    assert "PDT" in body or "PST" in body
    # APC-91: the rule is the client's now; the chip states the grace, not a caveat.
    assert "client to confirm" not in body
    assert "15-min grace · full hours" in body
    assert "Over by" in body


def test_a_transfer_review_labels_the_wait_not_the_overrun(staff):
    _lead, trip = _order()
    Reservation.objects.filter(pk=trip.pk).update(trip_type=Reservation.TripType.TRANSFER)

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Waited" in body
    assert "Over by" not in body
    # A pickup alone yields a suggestion here, so "Suggested" keys off the clock, not duration.
    assert "state.over_minutes === null ? '—' : state.suggested_minutes" in body
    assert "state.actual_minutes === null ? '—' : state.suggested_minutes" not in body


def test_an_in_house_trip_shows_driver_pay_and_no_payable(staff):
    _lead, trip = _order(farmed_out=False)

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Total time on the job" in body
    driver = trip.assignments.get().driver
    assert f"Set an hourly rate on {driver.name}" in body
    assert reverse("fleet:driver_edit", args=[driver.pk]) in body
    assert "Affiliate overtime" not in body
    assert "Payable" not in body


def test_a_payments_user_can_work_the_payable_in_the_pane(client):
    client.force_login(UserFactory(can_manage_payments=True))
    _lead, trip = _order()

    body = client.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Invoice missing" in body or "invoice_missing" in body
    assert "Approve payable" in body and "Mark paid" in body
    assert "Save invoice" in body


def test_a_cancelled_trip_has_nothing_to_review(staff):
    lead, _trip = _order()
    cancelled = ReservationFactory(lead=lead, trip_status=Reservation.TripStatus.CANCELLED)

    assert staff.get(_trip_url("trip_review_trip", cancelled)).status_code == 404


def test_the_order_page_shows_the_wedding_fee_acknowledgment(staff):
    from apps.leads.factories import ServiceTypeFactory

    lead, trip = _order()
    Reservation.objects.filter(pk=trip.pk).update(
        service_type=ServiceTypeFactory(name="Wedding Transportation")
    )

    before = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()
    Lead.objects.filter(pk=lead.pk).update(
        fees_acknowledged_at=timezone.now(), fees_acknowledged_version="feed1234"
    )
    after = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Cleaning fee and overtime not yet acknowledged" in before
    assert "Cleaning fee and overtime acknowledged" in after


def test_a_trip_whose_order_isnt_in_review_404s(staff):
    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))

    assert staff.get(_trip_url("trip_review_trip", trip)).status_code == 404


def test_saving_times_entered_in_the_trips_zone(staff):
    _lead, trip = _order()
    day = trip.pickup_date

    resp = staff.post(
        _trip_url("trip_review_save", trip),
        {
            "pickup_date": day.isoformat(),
            "pickup_time": "22:05",
            "dropoff_date": (day + timedelta(days=1)).isoformat(),
            "dropoff_time": "01:45",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["review"]
    review = TripReview.objects.get(reservation=trip)
    local = review.actual_dropoff_local
    assert (local.date(), local.hour, local.minute) == (day + timedelta(days=1), 1, 45)
    assert data["actual_minutes"] == 220
    assert data["suggested_minutes"] == 60  # 40 over, 15-min grace, a full hour
    assert (data["basis"], data["grace_minutes"]) == ("hourly", 15)


def test_half_a_time_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_save", trip), {"pickup_date": "2026-09-25"})

    assert resp.status_code == 400
    assert "date and the time" in resp.json()["error"]


def test_saving_the_customer_decision_and_driver_pay_separately(staff):
    _lead, trip = _order()
    url = _trip_url("trip_review_save", trip)
    start = trip.pickup_at
    review = reviews.review_for(trip)
    reviews.save_review(
        review,
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=40),
    )

    staff.post(url, {"billable_minutes": "30", "waived": "0"})
    resp = staff.post(url, {"override": "60", "override_note": "Waited at the FBO"})

    data = resp.json()["review"]
    assert data["billable_minutes"] == 30
    assert data["customer_amount"] == "168.00"  # 30 min bills the full hour
    # 30 billed min bill the hour: $200 × the 70% share would be $140; the adjustment replaces it.
    assert data["affiliate_overtime_derived"] == "140.00"
    assert data["affiliate_overtime_overridden"] is True
    assert data["expected_affiliate_amount"] == "480.00"
    assert data["decided"] is True


def test_an_adjustment_without_a_note_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_save", trip), {"override": "60"})

    assert resp.status_code == 400
    assert "note" in resp.json()["error"]


def test_waiving_without_a_reason_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_save", trip), {"billable_minutes": "30", "waived": "1"}
    )

    assert resp.status_code == 400
    assert "reason" in resp.json()["error"]


def test_rating_the_affiliate(staff):
    _lead, trip = _order()

    staff.post(_trip_url("trip_review_save", trip), {"rating": "4"})

    assert TripReview.objects.get(reservation=trip).affiliate_rating == 4


def test_completing_from_the_pop_up(staff):
    _lead, trip = _order()
    start = trip.pickup_at
    reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3),
        billable_overtime_minutes=0,
    )

    resp = staff.post(_trip_url("trip_review_complete", trip))

    assert resp.status_code == 200
    assert Task.objects.get(reservation=trip, kind="ops_review").status == Task.Status.DONE


def test_completing_without_a_decision_explains_why(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_complete", trip))

    assert resp.status_code == 400
    assert "actual" in resp.json()["error"]


def test_logging_and_resolving_an_issue(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_issue_add", trip),
        {"category": "complaint", "severity": "high", "note": "Left early"},
    )
    assert resp.status_code == 200
    issue = TripIssue.objects.get()

    resp = staff.post(reverse("trip_review_issue_resolve", args=[issue.pk]))

    assert resp.status_code == 200
    issue.refresh_from_db()
    assert issue.resolved_at is not None


def test_an_issue_needs_a_note(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_issue_add", trip), {"category": "driver", "severity": "low"}
    )

    assert resp.status_code == 400


def test_the_saved_times_come_back_in_the_form(staff):
    _lead, trip = _order()
    reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=datetime(2026, 9, 26, 5, 5, tzinfo=UTC),  # 10:05 PM PDT
        actual_dropoff_at=datetime(2026, 9, 26, 8, 45, tzinfo=UTC),
    )

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert 'value="22:05"' in body and 'value="01:45"' in body


# --- APC-101: each trip's money is settled from its pop-up ------------------------------
from unittest.mock import MagicMock  # noqa: E402

import stripe  # noqa: E402

from apps.payments import ledger  # noqa: E402
from apps.payments.models import Charge, JournalEntry  # noqa: E402


@pytest.fixture
def accountant(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def _owing(card=True):
    """An order in review, paid in full at booking, whose trip then grew by an hour."""
    lead, trip = _order()
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    plan = lead.payment  # `_order` already made one
    plan.quote_total = trip.line_total
    plan.stripe_customer_id = "cus_1" if card else ""
    plan.stripe_payment_method_id = "pm_1" if card else ""
    plan.card_brand = "visa" if card else ""
    plan.card_last4 = "4242" if card else ""
    plan.save()
    # Paid in full at booking: the Stripe charge row (what allocation reads) and its cash.
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=trip.line_total,
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_full",
        idempotency_key="bal",
    )
    ledger.post_capture(
        lead=lead,
        amount=trip.line_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="paid",
    )
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])
    return lead, plan, trip


def _pi(pi_id="pi_trip", amount=16800):
    return MagicMock(
        id=pi_id,
        status="succeeded",
        amount=amount,
        client_secret=f"{pi_id}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def test_the_trip_popup_shows_what_the_trip_owes_and_the_charge_button(accountant):
    _lead, _plan, trip = _owing()

    body = accountant.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "data-trip-money" in body
    assert "168.00" in body  # the difference, in the component's options
    assert "chargeTrip()" in body and "sendTripLink()" in body and "refundTrip()" in body
    assert "<select" not in body.split("data-trip-money")[1].split("</div>")[0]


def test_without_payments_access_the_popup_only_reads_the_money(staff):
    _lead, _plan, trip = _owing()

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "data-trip-money" in body and "needs payments access" in body
    assert "chargeTrip()" not in body


def test_charge_endpoint_charges_that_trip_and_returns_the_new_figures(accountant):
    _lead, plan, trip = _owing()

    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_pi()),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_pi()),
    ):
        resp = accountant.post(_trip_url("trip_charge", trip))

    data = resp.json()
    assert resp.status_code == 200 and data["ok"] is True
    assert data["amount"] == "168.00" and data["money"]["difference"] == "0.00"
    assert plan.charges.get(kind=Charge.Kind.TRIP).reservation == trip


def test_the_money_endpoints_need_payments_access(staff):
    _lead, _plan, trip = _owing()
    for name in ("trip_charge", "trip_refund", "trip_pay_link"):
        assert staff.post(_trip_url(name, trip)).status_code == 403


def test_refund_endpoint_needs_a_reason(accountant):
    _lead, _plan, trip = _owing()
    trip.overtime_hours = Decimal("0")
    trip.save(update_fields=["overtime_hours"])
    TripReview.objects.filter(reservation=trip).update(overtime_waived=False)
    from apps.reservations.factories import TripExtraFactory

    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("50"))

    resp = accountant.post(_trip_url("trip_refund", trip), {"reason": ""})

    assert resp.status_code == 400 and "why" in resp.json()["error"]


def test_refund_endpoint_refunds_the_overpayment_with_the_reason(accountant):
    _lead, plan, trip = _owing()
    trip.overtime_hours = Decimal("0")
    trip.save(update_fields=["overtime_hours"])
    from apps.reservations.factories import TripExtraFactory

    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("50"))

    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_1")):
        resp = accountant.post(_trip_url("trip_refund", trip), {"reason": "Courtesy"})

    data = resp.json()
    assert resp.status_code == 200 and data["amount"] == "50.00"
    assert data["money"]["difference"] == "0.00"
    assert plan.charges.get(kind=Charge.Kind.REFUND).note == "Courtesy"


def test_pay_link_endpoint_opens_a_trip_intent_and_sends_the_link(accountant, settings):
    settings.OUTBOUND_ENABLED = True
    lead, plan, trip = _owing(card=False)
    lead.contact.phone = "+15715551212"
    lead.contact.save()

    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_pi("pi_link")),
        patch("apps.payments.services._send_to_customer", return_value="sms") as send,
    ):
        resp = accountant.post(_trip_url("trip_pay_link", trip))

    assert resp.status_code == 200 and resp.json()["channel"] == "sms"
    link = plan.charges.get(kind=Charge.Kind.TRIP)
    assert (link.reservation, link.status, link.amount) == (
        trip,
        Charge.Status.PENDING,
        Decimal("168.00"),
    )
    assert "/quote/" in send.call_args.kwargs["body"]


def test_a_plainly_cancelled_trip_cannot_be_settled(accountant):
    _lead, _plan, trip = _owing()
    trip.trip_status = Reservation.TripStatus.CANCELLED
    trip.save(update_fields=["trip_status"])

    assert accountant.post(_trip_url("trip_charge", trip)).status_code == 404


def test_the_order_page_no_longer_offers_a_final_bill(staff):
    lead, _plan, _trip = _owing()

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Charge card" not in body and "finalBilling" not in body
    assert "charged or refunded on its own" in body
