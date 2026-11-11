"""APC-135: trip statuses and the processed review report back to GNet.

`farm_in_push.deliver` is patched throughout, so pushes stay pending and nothing leaves
the test.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.dispatch import farm_in_push
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments import services as payments
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

Push = FarmInStatusPush
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch.object(farm_in_push, "deliver"),
    ):
        yield


@pytest.fixture
def accepted():
    return FarmInRequestFactory(
        status=FarmInRequest.Status.ACCEPTED,
        reservation=ReservationFactory(),
        transaction_id="tx-h",
    )


@pytest.fixture
def agent():
    return UserFactory(can_manage_payments=True)


def _codes(req):
    return list(Push.objects.filter(request=req).order_by("pk").values_list("status", flat=True))


def _trip(status=S.DONE):
    """A finished, booked hourly trip (the shape `test_processing` uses)."""
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory())
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_time=time(10, 0),
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_timezone="America/New_York",
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _paid(trip):
    """The order prepaid the trip's booked total by card."""
    plan = PaymentPlanFactory(
        lead=trip.lead,
        quote_total=trip.line_total,
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
        card_brand="visa",
        card_last4="4242",
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
    return plan


def _farmed_in(trip):
    return FarmInRequestFactory(
        status=FarmInRequest.Status.ACCEPTED, reservation=trip, transaction_id="tx-close"
    )


def _sent(req, status):
    Push.objects.filter(request=req, status=status).update(
        state=Push.State.SENT, sent_at=timezone.now()
    )


@pytest.mark.parametrize(
    "status,code",
    [
        ("dispatched", "ASSIGNED"),
        ("on_the_way", "EN_ROUTE"),
        ("arrived", "ON_LOCATION"),
        ("customer_in_car", "PASSENGER_ON_BOARD"),
        ("done", "COMPLETE"),
        ("cancelled", "CANCEL"),
        ("late_cancel", "CANCEL"),
        ("no_show", "NO_SHOW"),
    ],
)
def test_set_trip_status_enqueues_mapped_code(accepted, status, code):
    set_trip_status(accepted.reservation, status)

    pushes = list(Push.objects.filter(request=accepted))
    assert [(p.status, p.state, p.total_amount) for p in pushes] == [
        (code, Push.State.PENDING, None)
    ]


@pytest.mark.parametrize("status", ["unassigned", "assigned"])
def test_unmapped_status_enqueues_nothing(accepted, status):
    """Assigned is an offer still unanswered: GNet hears ASSIGNED only once we're Dispatched."""
    set_trip_status(accepted.reservation, status)

    assert not Push.objects.exists()


def test_an_offer_on_a_farmed_in_trip_reports_nothing_until_confirmed():
    from apps.dispatch import services
    from apps.vendors.factories import VendorFactory

    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), trip_status=S.UNASSIGNED)
    req = FarmInRequestFactory(
        status=FarmInRequest.Status.ACCEPTED, reservation=trip, transaction_id="tx-offer"
    )

    offer = services.send_offer(trip, VendorFactory(email=""), payout=Decimal("100"))
    trip.refresh_from_db()
    assert trip.trip_status == S.ASSIGNED
    assert _codes(req) == []

    services.confirm(offer)
    trip.refresh_from_db()
    assert trip.trip_status == S.DISPATCHED
    assert _codes(req) == ["ASSIGNED"]


def test_unchanged_status_enqueues_nothing(accepted):
    trip = accepted.reservation
    set_trip_status(trip, "on_the_way")
    _sent(accepted, "EN_ROUTE")

    assert set_trip_status(trip, "on_the_way") is None
    assert _codes(accepted) == ["EN_ROUTE"]


def test_status_on_a_request_not_accepted_enqueues_nothing():
    req = FarmInRequestFactory(
        status=FarmInRequest.Status.PENDING, reservation=ReservationFactory()
    )

    set_trip_status(req.reservation, "dispatched")

    assert not Push.objects.exists()


def test_hook_costs_one_query_on_ordinary_trip():
    """The lookup is a single SELECT ... FOR UPDATE. Its atomic block nests inside the
    test's transaction, so SAVEPOINT / RELEASE statements are not counted."""
    trip = ReservationFactory()

    def counted(ctx):
        return [
            q["sql"]
            for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith(("SAVEPOINT", "RELEASE"))
        ]

    with CaptureQueriesContext(connection) as mapped:
        assert farm_in_push.after_trip_status(trip, "dispatched") is None
    with CaptureQueriesContext(connection) as unmapped:
        assert farm_in_push.after_trip_status(trip, "unassigned") is None

    assert len(counted(mapped)) == 1
    assert len(counted(unmapped)) == 0


def test_processing_pushes_close_with_final_total(agent):
    trip = _trip()
    _paid(trip)
    req = _farmed_in(trip)

    processing.process(trip, user=agent, expected="0.00")

    trip.refresh_from_db()
    close = Push.objects.get(request=req, status="CLOSE")
    assert close.state == Push.State.PENDING
    assert close.total_amount == trip.line_total == Decimal("600.00")


def test_processing_a_trip_that_is_not_farmed_in_pushes_nothing(agent):
    trip = _trip()
    _paid(trip)

    processing.process(trip, user=agent, expected="0.00")

    assert not Push.objects.exists()


def test_a_failing_close_push_never_undoes_the_lock(agent):
    trip = _trip()
    _paid(trip)
    _farmed_in(trip)

    with patch.object(farm_in_push, "after_review_processed", side_effect=RuntimeError("boom")):
        review = processing.process(trip, user=agent, expected="0.00")

    review.refresh_from_db()
    assert review.is_processed


def test_unlock_and_reprocess_do_not_push_close_twice(agent):
    trip = _trip()
    _paid(trip)
    req = _farmed_in(trip)
    processing.process(trip, user=agent, expected="0.00")
    _sent(req, "CLOSE")

    processing.unlock(trip, user=agent, reason="Wrong times")
    processing.process(trip, user=agent, expected="0.00")

    assert _codes(req) == ["CLOSE"]


def test_cancel_after_close_still_pushes_cancel(accepted):
    FarmInStatusPushFactory(
        request=accepted, status="CLOSE", state=Push.State.SENT, total_amount=Decimal("165")
    )

    set_trip_status(accepted.reservation, "cancelled")

    assert _codes(accepted) == ["CLOSE", "CANCEL"]
    assert Push.objects.get(status="CANCEL").state == Push.State.PENDING


def test_assigned_twice_pushes_twice_once_first_is_sent(accepted):
    trip = accepted.reservation
    set_trip_status(trip, "dispatched")
    set_trip_status(trip, "unassigned")
    set_trip_status(trip, "dispatched")
    assert _codes(accepted) == ["ASSIGNED"]  # the first is still pending

    _sent(accepted, "ASSIGNED")
    set_trip_status(trip, "unassigned")
    set_trip_status(trip, "dispatched")

    assert _codes(accepted) == ["ASSIGNED", "ASSIGNED"]


def test_final_reported_reads_sent_close_only():
    trip = ReservationFactory()
    req = FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED, reservation=trip)
    FarmInStatusPushFactory(request=req, status="CLOSE", state=Push.State.PENDING, total_amount=1)
    FarmInStatusPushFactory(request=req, status="CLOSE", state=Push.State.FAILED, total_amount=2)
    FarmInStatusPushFactory(request=req, status="COMPLETE", state=Push.State.SENT, total_amount=3)
    assert farm_in_push.final_reported(trip) is None

    FarmInStatusPushFactory(
        request=req, status="CLOSE", state=Push.State.SENT, total_amount=Decimal("165")
    )

    assert farm_in_push.final_reported(trip) == Decimal("165.00")
    assert farm_in_push.final_reported(ReservationFactory()) is None


def test_cancelling_an_accepted_farm_in_trip_queues_cancel():
    trip = _trip(S.ASSIGNED)
    req = _farmed_in(trip)
    payments.cancel_trips(
        trip.lead, [trip], user=UserFactory(role=User.Role.OWNER_ADMIN), reason="Customer cancelled"
    )
    trip.refresh_from_db()
    assert trip.trip_status == S.CANCELLED
    assert list(Push.objects.filter(request=req).values_list("status", "state")) == [
        ("CANCEL", Push.State.PENDING)
    ]


def test_a_failing_farm_in_hook_never_breaks_a_cancel():
    trip = _trip(S.ASSIGNED)
    _farmed_in(trip)
    with patch.object(farm_in_push, "after_trip_status", side_effect=RuntimeError("boom")):
        payments.cancel_booked_order(trip.lead)
    trip.refresh_from_db()
    assert trip.trip_status == S.CANCELLED
