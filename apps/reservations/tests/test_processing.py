"""APC-102 — Trip Processing: each finished trip worked on its own (spec 2026-10-25 §4).

Stripe and Podium are mocked throughout; nothing leaves the test.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
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
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _trip(status=S.DONE, *, days_ago=1, contact=None, lead=None, **kw):
    lead = lead or LeadFactory(status=Lead.Status.BOOKED, contact=contact or ContactFactory())
    kw.setdefault("trip_type", "hourly")
    kw.setdefault("rate", Decimal("200"))
    kw.setdefault("hours", Decimal("3"))
    kw.setdefault("min_hours", 0)
    kw.setdefault("pickup_time", time(10, 0))
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_timezone="America/New_York",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _ready(**query):
    return processing.ready_trips(processing.Filters.from_query(query))


# --- Task 1: what is ready ---------------------------------------------------------------


@pytest.mark.parametrize("status", [S.DONE, S.LATE_CANCEL, S.NO_SHOW])
def test_done_late_cancel_and_no_show_are_ready(status):
    trip = _trip(status)
    assert trip in _ready()


@pytest.mark.parametrize("status", [S.CANCELLED, S.COVID_CANCELLATION])
def test_plain_cancelled_and_covid_never_are(status):
    assert _trip(status) not in _ready()


def test_cancelled_by_affiliate_is_ready_only_with_money_to_settle():
    owes = _trip(S.CANCELLED_BY_AFFILIATE)
    free = _trip(S.CANCELLED_BY_AFFILIATE, rate=Decimal("0"))

    ready = _ready()

    assert owes in ready and free not in ready


def test_an_ended_trip_with_no_status_is_ready_and_needs_a_status():
    trip = _trip("", days_ago=1)

    (found,) = [t for t in _ready() if t.pk == trip.pk]

    assert found.needs_status is True


def test_a_trip_inside_the_grace_with_no_status_is_not_ready():
    trip = _trip("", days_ago=0, pickup_time=None)
    trip.pickup_time = (timezone.localtime() - timedelta(minutes=10)).time()
    trip.save(update_fields=["pickup_time"])

    assert trip not in _ready()


def test_a_processed_trip_leaves_ready_and_shows_in_processed():
    trip = _trip()
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    assert trip not in _ready()
    assert trip in processing.processed_trips(processing.Filters.from_query({"view": "processed"}))


def test_only_booked_orders_count():
    quoted = _trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    booked = _trip(days_ago=2)

    ready = _ready()
    assert booked in ready and quoted not in ready


def test_ready_has_no_rolling_window_but_starts_at_the_processing_date():
    """A trip never drops off Ready unprocessed; trips before `processing_from` (settled the
    old way before the deploy) never show."""
    from apps.tasks.models import TaskConfig

    old = _trip(days_ago=60)
    before = _trip(days_ago=400)
    config = TaskConfig.load()
    config.processing_from = timezone.localdate() - timedelta(days=90)
    config.save()

    ready = _ready()
    assert old in ready and before not in ready


def test_the_date_filter_narrows_ready_and_processed_defaults_to_30_days():
    old = _trip(days_ago=60)
    recent = _trip(days_ago=2)
    assert _ready(**{"from": (timezone.localdate() - timedelta(days=7)).isoformat()}) == [recent]
    for t in (old, recent):
        TripReview.objects.create(reservation=t, processed_at=timezone.now())

    shown = processing.processed_trips(processing.Filters.from_query({"view": "processed"}))
    assert shown == [recent]


def test_a_trip_left_at_a_live_status_after_it_ended_needs_a_status():
    """The parallel-run gap: LA never sent Done, the trip sits at Customer in car."""
    trip = _trip(S.CUSTOMER_IN_CAR, days_ago=1)

    (found,) = [t for t in _ready() if t.pk == trip.pk]

    assert found.needs_status is True


def test_the_issues_filter_shows_trips_with_an_open_issue():
    from apps.reservations.models import TripIssue

    flagged = _trip()
    _trip()
    review = TripReview.objects.create(reservation=flagged)
    TripIssue.objects.create(review=review, category="driver", severity="low", note="Late")

    assert _ready(issues="1") == [flagged]


@pytest.mark.parametrize(
    "status,days_ago,expected",
    [
        (S.DONE, 1, True),
        (S.NO_SHOW, 1, True),
        (S.LATE_CANCEL, 1, True),
        (S.CANCELLED_BY_AFFILIATE, 1, True),
        (S.CANCELLED, 1, False),
        ("", 1, True),
        ("", -2, False),
    ],
)
def test_workable(status, days_ago, expected):
    assert processing.workable(_trip(status, days_ago=days_ago)) is expected


def test_a_trip_on_an_order_that_is_not_booked_is_not_workable():
    assert processing.workable(_trip(lead=LeadFactory(status=Lead.Status.LOST))) is False


def test_search_by_customer_name_or_trip_reference():
    mine = _trip(contact=ContactFactory(name="Priya Okafor"))
    other = _trip(contact=ContactFactory(name="Marcus Bell"))

    assert _ready(q="okafor") == [mine]
    assert _ready(q=other.reference.lower()) == [other]


def test_groups_are_customers_sorted_by_their_oldest_trip():
    a = ContactFactory(name="A")
    b = ContactFactory(name="B")
    a2 = _trip(contact=a, days_ago=1)
    _trip(contact=b, days_ago=5)
    a1 = _trip(lead=a2.lead, days_ago=3)
    PaymentPlanFactory(lead=a2.lead, card_last4="4242", stripe_payment_method_id="pm_1")

    groups = processing.group_by_customer(_ready())

    assert [g.contact for g in groups] == [b, a]
    assert groups[1].trips == [a1, a2]
    assert groups[1].pays == "Card ••4242" and groups[0].pays == "No card"


def test_ready_trips_cost_a_flat_number_of_queries():
    _trip()
    _trip(S.CANCELLED_BY_AFFILIATE, contact=ContactFactory())

    def touch():
        for g in processing.group_by_customer(_ready()):
            _ = g.pays
            for t in g.trips:
                t.line_total, t.reference, list(t.stops.all()), t.active_list

    touch()  # the first run creates the TaskConfig singleton; measure after
    with CaptureQueriesContext(connection) as one:
        touch()
    for _ in range(50):
        _trip(contact=ContactFactory())
    with CaptureQueriesContext(connection) as many:
        touch()

    assert len(many) == len(one), [q["sql"][:120] for q in many.captured_queries]


# --- Task 3: process, unlock, add a charge ------------------------------------------------
from unittest.mock import MagicMock  # noqa: E402

import stripe  # noqa: E402

from apps.accounts.factories import UserFactory  # noqa: E402
from apps.dispatch.factories import AssignmentFactory  # noqa: E402
from apps.dispatch.models import Assignment  # noqa: E402
from apps.payments import ledger, trip_money  # noqa: E402
from apps.payments.models import AffiliatePayable, Charge, JournalEntry  # noqa: E402
from apps.reservations import reviews  # noqa: E402
from apps.reservations.models import ProcessingEvent  # noqa: E402
from apps.tasks.models import Task  # noqa: E402


@pytest.fixture
def agent():
    return UserFactory(can_manage_payments=True)


def _paid(trip, *, card=True, extra_hours=Decimal("0")):
    """The order prepaid the trip's booked total; then the trip grows by `extra_hours`."""
    plan = PaymentPlanFactory(
        lead=trip.lead,
        quote_total=trip.line_total,
        stripe_customer_id="cus_1" if card else "",
        stripe_payment_method_id="pm_1" if card else "",
        card_brand="visa" if card else "",
        card_last4="4242" if card else "",
    )
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
    if extra_hours:
        trip.overtime_hours = extra_hours
        trip.save(update_fields=["overtime_hours"])
    return plan


def _intent(pi="pi_trip", amount=20000):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        client_secret=f"{pi}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _diff(trip):
    return trip_money.money_for(Reservation.objects.get(pk=trip.pk)).difference


def test_processing_a_paid_trip_locks_it_and_records_who(agent):
    trip = _trip()
    _paid(trip)

    review = processing.process(trip, user=agent, expected="0.00")

    assert review.is_processed and review.processed_by == agent
    assert review.completed_at is not None
    assert [e.kind for e in review.events.all()] == [ProcessingEvent.Kind.PROCESSED]


def test_processing_charges_the_difference_to_the_card_when_asked(agent):
    trip = _trip()
    plan = _paid(trip, extra_hours=Decimal("1"))  # +200

    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as create,
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()),
    ):
        review = processing.process(trip, user=agent, expected="200.00", collect="card")

    assert create.call_args.kwargs["amount"] == 20000
    assert review.is_processed
    assert plan.charges.get(kind=Charge.Kind.TRIP).status == Charge.Status.SUCCEEDED
    assert [e.kind for e in review.events.all()] == ["charged", "processed"]
    assert _diff(trip) == Decimal("0.00")


def test_a_declined_card_leaves_the_trip_unprocessed(agent):
    trip = _trip()
    plan = _paid(trip, extra_hours=Decimal("1"))
    err = stripe.error.CardError("Your card was declined.", "card", "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        with pytest.raises(processing.ProcessingError, match="declined"):
            processing.process(trip, user=agent, expected="200.00", collect="card")

    assert not TripReview.objects.get(reservation=trip).is_processed
    # The failed attempt is kept (its key is spent), so a retry makes a new one.
    assert plan.charges.get(kind=Charge.Kind.TRIP).status == Charge.Status.FAILED


def test_an_owing_trip_needs_a_way_to_collect(agent):
    trip = _trip()
    _paid(trip, extra_hours=Decimal("1"))

    with pytest.raises(processing.ProcessingError, match="Charge the card or send"):
        processing.process(trip, user=agent, expected="200.00")


def test_processing_with_no_card_sends_the_link_and_locks(agent, settings):
    settings.OUTBOUND_ENABLED = True
    trip = _trip()
    plan = _paid(trip, card=False, extra_hours=Decimal("1"))

    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent("pi_link")),
        patch.object(stripe.Customer, "create", return_value=MagicMock(id="cus_new")),
        patch("apps.payments.services._send_to_customer", return_value="sms") as send,
    ):
        review = processing.process(
            trip, user=agent, expected="200.00", collect="link", base_url="https://x"
        )

    assert review.is_processed
    assert send.called and trip.reference in send.call_args.kwargs["body"]
    link = plan.charges.get(kind=Charge.Kind.TRIP)
    assert link.status == Charge.Status.PENDING and link.amount == Decimal("200.00")
    assert [e.kind for e in review.events.all()] == ["pay_link_sent", "processed"]


def test_the_link_path_refuses_before_any_intent_while_outbound_is_off(agent, settings):
    settings.OUTBOUND_ENABLED = False
    trip = _trip()
    plan = _paid(trip, card=False, extra_hours=Decimal("1"))

    with patch.object(stripe.PaymentIntent, "create") as create:
        with pytest.raises(processing.ProcessingError, match="Outbound"):
            processing.process(trip, user=agent, expected="200.00", collect="link")

    create.assert_not_called()
    assert not plan.charges.filter(kind=Charge.Kind.TRIP).exists()


def test_processing_refunds_an_overpayment_with_a_reason(agent):
    from apps.reservations.factories import TripExtraFactory

    trip = _trip()
    _paid(trip)
    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("50"))

    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_1")):
        with pytest.raises(processing.ProcessingError, match="why"):
            processing.process(trip, user=agent, expected="-50.00", collect="refund")
        review = processing.process(
            trip, user=agent, expected="-50.00", collect="refund", reason="Late pickup"
        )

    assert review.is_processed and _diff(trip) == Decimal("0.00")
    assert review.events.last().note == "Late pickup"


def test_an_off_card_overpayment_can_be_kept_as_credit(agent):
    from apps.reservations.factories import TripExtraFactory

    trip = _trip()
    _paid(trip)
    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("50"))

    review = processing.process(
        trip, user=agent, expected="-50.00", collect="keep", reason="Paid by check; on account"
    )

    assert review.is_processed
    assert "kept as credit" in review.events.last().note


def test_an_undecided_suggestion_must_be_decided_first(agent):
    trip = _trip()
    _paid(trip)
    review = reviews.review_for(trip)
    TripReview.objects.filter(pk=review.pk).update(suggested_overtime_minutes=60)

    with pytest.raises(processing.ProcessingError, match="Decide the overtime"):
        processing.process(trip, user=agent, expected="0.00")


def test_a_stale_pane_is_refused(agent):
    trip = _trip()
    _paid(trip, extra_hours=Decimal("1"))

    with pytest.raises(processing.ProcessingError, match="price changed"):
        processing.process(trip, user=agent, expected="0.00", collect="card")


def test_a_trip_that_needs_a_status_cannot_be_processed(agent):
    trip = _trip(S.CUSTOMER_IN_CAR)
    _paid(trip)

    with pytest.raises(processing.ProcessingError, match="status"):
        processing.process(trip, user=agent, expected="0.00")


def test_processing_twice_is_refused_and_charges_once(agent):
    trip = _trip()
    plan = _paid(trip, extra_hours=Decimal("1"))
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent()) as create,
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()),
    ):
        processing.process(trip, user=agent, expected="200.00", collect="card")
        with pytest.raises(processing.ProcessingError):
            processing.process(trip, user=agent, expected="200.00", collect="card")

    assert create.call_count == 1
    assert plan.charges.filter(kind=Charge.Kind.TRIP).count() == 1


def test_a_second_charge_for_money_already_collected_is_refused(agent):
    """Two agents on one trip: the second charge_trip finds nothing left to collect."""
    trip = _trip()
    _paid(trip, extra_hours=Decimal("1"))
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent()),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()),
    ):
        trip_money.charge_trip(trip, Decimal("200.00"))
        with pytest.raises(Exception, match="Already charged"):
            trip_money.charge_trip(trip, Decimal("200.00"))


def test_processing_freezes_driver_pay_and_creates_the_payable(agent):
    trip = _trip()
    _paid(trip)
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    processing.process(trip, user=agent, expected="0.00")

    assert AffiliatePayable.objects.filter(assignment__reservation=trip).exists()


def test_a_no_show_with_no_times_is_processed(agent):
    trip = _trip(S.NO_SHOW)
    _paid(trip)

    review = processing.process(trip, user=agent, expected="0.00")

    assert review.is_processed and review.actual_pickup_at is None


def test_processing_closes_the_trips_review_tasks_only(agent):
    trip = _trip()
    other = _trip(lead=trip.lead, days_ago=0)
    _paid(trip)
    now = timezone.now()
    mine = Task.objects.create(lead=trip.lead, reservation=trip, kind="ops_review", opens_at=now)
    theirs = Task.objects.create(lead=trip.lead, reservation=other, kind="ops_review", opens_at=now)

    processing.process(trip, user=agent, expected="0.00")

    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.is_closed and not theirs.is_closed


def test_processing_trues_up_revenue(agent):
    trip = _trip()
    _paid(trip)
    ledger.recognize_reservation(trip)
    from apps.reservations.factories import TripExtraFactory

    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("50"))
    processing.process(trip, user=agent, expected="-50.00", collect="keep", reason="On account")

    assert ledger.order_balances(trip.lead)["recognized"] == Decimal("550.00")


def test_unlock_needs_a_reason_records_it_and_reopens_the_review_task(agent):
    trip = _trip()
    _paid(trip)
    task = Task.objects.create(
        lead=trip.lead, reservation=trip, kind="ops_review", opens_at=timezone.now()
    )
    processing.process(trip, user=agent, expected="0.00")

    with pytest.raises(processing.ProcessingError, match="why"):
        processing.unlock(trip, user=agent, reason=" ")
    review = processing.unlock(trip, user=agent, reason="Customer called about the wait")

    assert not review.is_processed and review.unlock_reason.startswith("Customer called")
    assert review.events.last().kind == ProcessingEvent.Kind.UNLOCKED
    task.refresh_from_db()
    assert not task.is_closed


def test_add_charge_adds_a_line_and_charges_exactly_it(agent):
    trip = _trip()
    plan = _paid(trip)
    processing.process(trip, user=agent, expected="0.00")

    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent(amount=5000)) as create,
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent(amount=5000)),
    ):
        processing.add_charge(trip, user=agent, amount="50", label="Stain on the seat")

    assert create.call_args.kwargs["amount"] == 5000
    trip.refresh_from_db()
    assert trip.extras.get().label == "Stain on the seat"
    assert plan.charges.get(kind=Charge.Kind.TRIP).note == "Stain on the seat"
    assert _diff(trip) == Decimal("0.00")


def test_add_charge_removes_the_line_when_the_card_declines(agent):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=agent, expected="0.00")
    err = stripe.error.CardError("Declined.", "card", "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        with pytest.raises(processing.ProcessingError, match="Declined"):
            processing.add_charge(trip, user=agent, amount="50", label="Stain")

    trip.refresh_from_db()
    assert not trip.extras.exists() and trip.extras_total == Decimal("0.00")


# --- Task 4: the lock holds -----------------------------------------------------------------
import json  # noqa: E402

from django.urls import reverse  # noqa: E402

from apps.reservations.editor import reservation_draft  # noqa: E402


def _processed(agent):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=agent, expected="0.00")
    return trip


def test_the_editor_refuses_a_processed_trip(client, agent):
    trip = _processed(agent)
    client.force_login(agent)
    draft = {**reservation_draft(trip), "lead_id": trip.lead_id, "rate": 999}

    resp = client.post(
        reverse("reservation_save"), json.dumps(draft), content_type="application/json"
    )

    assert resp.status_code == 400
    assert "Unlock it from Trip Processing" in resp.content.decode()
    trip.refresh_from_db()
    assert trip.rate == Decimal("200.00")


def test_apply_to_group_refuses_when_a_sibling_is_processed(client, agent):
    from apps.reservations import groups

    trip = _trip()
    _paid(trip)
    groups.set_group_size(trip, 2)
    sibling = trip.lead.reservations.exclude(pk=trip.pk).get()
    TripReview.objects.update_or_create(
        reservation=sibling, defaults={"processed_at": timezone.now()}
    )
    client.force_login(agent)
    trip.refresh_from_db()
    draft = {**reservation_draft(trip), "lead_id": trip.lead_id, "applyToGroup": True}

    resp = client.post(
        reverse("reservation_save"), json.dumps(draft), content_type="application/json"
    )

    assert resp.status_code == 400 and "processed" in resp.content.decode()


def test_a_processed_trip_cannot_be_deleted(client, agent):
    trip = _processed(agent)
    client.force_login(agent)

    client.post(reverse("reservation_delete", args=[trip.pk]))

    assert Reservation.objects.filter(pk=trip.pk).exists()


def test_times_and_overtime_are_locked_but_issues_and_rating_are_not(agent):
    trip = _processed(agent)
    review = TripReview.objects.get(reservation=trip)

    with pytest.raises(reviews.ReviewError, match="processed"):
        reviews.save_review(review, user=agent, billable_overtime_minutes=60)
    with pytest.raises(reviews.ReviewError, match="processed"):
        reviews.save_review(review, user=agent, actual_pickup_at=timezone.now())
    reviews.add_issue(review, category="driver", severity="low", note="Late", user=agent)


def test_the_pop_up_money_actions_refuse_a_processed_trip(client, agent):
    trip = _processed(agent)
    client.force_login(agent)

    resp = client.post(reverse("trip_charge", args=[trip.pk]))

    assert resp.status_code == 400 and "processed" in resp.json()["error"]


def test_a_no_show_can_be_worked_in_the_pane(client, agent):
    """The pane's endpoints used to 404 every 'Cancelled'-phase trip; a no-show is
    exactly what processing settles."""
    trip = _trip(S.NO_SHOW)
    client.force_login(agent)

    resp = client.get(reverse("trip_review_trip", args=[trip.pk]))

    assert resp.status_code == 200
