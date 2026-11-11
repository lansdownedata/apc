"""The app events that queue messages (spec C section 3): every caller goes through
`messaging.events`, and the new event hooks queue the rows an Admin adds on them.

No seeded row uses the new triggers (deposit paid, balance paid, affiliate accepts, trip
cancelled, trip done), so nothing anyone receives changes on day one; each test adds a row.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.integrations.podium import PodiumAPIError
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import MessageRowFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.payments import services as payments
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = TouchPoint.Status
SEND = "apps.integrations.podium.send_message"
DRIVER = {
    "driver_name": "Sam Rivera",
    "driver_cell": "+15715551212",
    "vehicle_desc": "Suburban",
    "vehicle_number": "9",
}


@pytest.fixture
def live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"


def _added(trigger: str, **kw) -> MessageTemplate:
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("sms_body", "Text for {first_name}")
    kw.setdefault("send_email", False)
    return MessageRowFactory(trigger=trigger, **kw)


def _trip(*, status=Lead.Status.BOOKED, hours=24 * 10, **kw) -> Reservation:
    lead = LeadFactory(status=status)
    lead.contact.phone = "+15715550001"
    lead.contact.save()
    res = ReservationFactory(lead=lead, pickup_date=timezone.now().date(), pickup_time=None, **kw)
    when = (timezone.now() + timedelta(hours=hours)).astimezone(res.pickup_at.tzinfo)
    res.pickup_date, res.pickup_time = when.date(), when.time().replace(microsecond=0)
    res.save()
    return res


def _queued(row: MessageTemplate, **kw):
    return TouchPoint.objects.filter(message=row, **kw)


def _intent(pi_id: str, amount: int):
    return MagicMock(
        id=pi_id,
        status="succeeded",
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _pay(plan, pi_id: str, kind: str, amount: int = 50000) -> Charge:
    with (
        patch.object(
            payments.stripe.PaymentIntent, "retrieve", return_value=_intent(pi_id, amount)
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        return payments.record_payment(plan, pi_id, kind=kind)


def _plan(status=Lead.Status.QUOTED):
    return PaymentPlanFactory(
        lead=LeadFactory(status=status),
        quote_total=Decimal("1000.00"),
        deposit_pct=50,
        stripe_customer_id="cus_1",
    )


# --- payments ----------------------------------------------------------------------------


def test_a_deposit_paid_queues_the_deposit_paid_rows():
    row = _added("deposit_paid")
    other = _added("balance_paid")
    plan = _plan()
    _pay(plan, "pi_dep", Charge.Kind.DEPOSIT)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation, tp.kind, tp.status) == (plan.lead, None, row.key, S.SCHEDULED)
    assert not _queued(other).exists()


def test_a_deposit_recorded_twice_is_one_message():
    row = _added("deposit_paid")
    plan = _plan()
    _pay(plan, "pi_dep", Charge.Kind.DEPOSIT)
    _queued(row).update(status=S.SENT)
    _pay(plan, "pi_dep", Charge.Kind.DEPOSIT)  # the webhook after the pay page
    assert _queued(row).count() == 1


def test_a_deposit_paid_is_not_withdrawn_by_the_booking_it_causes():
    row = _added("deposit_paid")
    plan = _plan()
    _pay(plan, "pi_dep", Charge.Kind.DEPOSIT)
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    assert _queued(row, status=S.SCHEDULED).count() == 1


def test_a_balance_paid_queues_the_balance_paid_rows():
    row = _added("balance_paid")
    other = _added("deposit_paid")
    plan = _plan(status=Lead.Status.BOOKED)
    _pay(plan, "pi_bal", Charge.Kind.BALANCE)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (plan.lead, None)
    assert not _queued(other).exists()


def _balance_due_plan():
    plan = _plan(status=Lead.Status.BOOKED)
    plan.stripe_payment_method_id = "pm_1"
    plan.balance_status = "scheduled"
    plan.save(update_fields=["stripe_payment_method_id", "balance_status", "updated_at"])
    return plan


def _charge_balance(plan, **create):
    create.setdefault("return_value", MagicMock(id="pi_bal", status="succeeded", amount=50000))
    with patch.object(payments.stripe.PaymentIntent, "create", **create):
        return payments.charge_balance(plan)


def test_the_balance_cron_charging_the_card_is_a_balance_paid():
    row = _added("balance_paid")
    plan = _balance_due_plan()
    _charge_balance(plan)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (plan.lead, None)


def test_the_webhook_for_a_cron_charged_balance_does_not_tell_them_again():
    row = _added("balance_paid")
    plan = _balance_due_plan()
    _charge_balance(plan)
    _queued(row).update(status=S.CANCELLED)  # nothing left to block a second one
    _pay(plan, "pi_bal", Charge.Kind.BALANCE)  # payment_intent.succeeded arrives
    assert _queued(row).count() == 1


def test_a_declined_balance_is_not_a_balance_paid():
    import stripe

    row = _added("balance_paid")
    plan = _balance_due_plan()
    declined = stripe.error.CardError("Your card was declined.", None, "card_declined")
    _charge_balance(plan, side_effect=declined)
    assert not _queued(row).exists()


def test_a_trip_charge_is_neither_payment_event():
    rows = [_added("deposit_paid"), _added("balance_paid")]
    plan = _plan(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=plan.lead)
    with (
        patch.object(
            payments.stripe.PaymentIntent,
            "retrieve",
            return_value=MagicMock(
                id="pi_trip",
                status="succeeded",
                amount=10000,
                metadata={"reservation_id": str(trip.pk)},
                payment_method=None,
            ),
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        payments.record_payment(plan, "pi_trip", kind=Charge.Kind.TRIP)
    assert not TouchPoint.objects.filter(message__in=rows).exists()


# --- dispatch ----------------------------------------------------------------------------


def test_an_affiliate_accepting_queues_the_affiliate_accepted_rows():
    row = _added("affiliate_accepted")
    trip = _trip()
    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    dispatch_services.confirm(offer)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (trip.lead, trip)


def test_coverage_arranged_by_phone_is_an_affiliate_accepting():
    from apps.vendors.factories import VendorFactory

    row = _added("affiliate_accepted")
    trip = _trip()
    dispatch_services.assign_direct(trip, VendorFactory(), payout=Decimal("100.00"))
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (trip.lead, trip)


def test_in_house_coverage_is_not_an_affiliate_accepting():
    from apps.fleet.factories import DriverFactory

    row = _added("affiliate_accepted")
    trip = _trip()
    dispatch_services.assign_in_house(trip, DriverFactory())
    assert not _queued(row).exists()


# --- trip status -------------------------------------------------------------------------


def test_a_cancelled_trip_queues_the_trip_cancelled_rows():
    row = _added("trip_cancelled")
    trip = _trip()
    set_trip_status(trip, Reservation.TripStatus.CANCELLED)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (trip.lead, trip)


def test_moving_between_cancelled_statuses_is_not_a_second_cancellation():
    row = _added("trip_cancelled")
    trip = _trip()
    set_trip_status(trip, Reservation.TripStatus.CANCELLED)
    _queued(row).update(status=S.SENT)
    set_trip_status(trip, Reservation.TripStatus.LATE_CANCEL)
    assert _queued(row).count() == 1


def test_a_done_trip_queues_the_trip_done_rows():
    row = _added("trip_done")
    trip = _trip()
    set_trip_status(trip, Reservation.TripStatus.DONE)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (trip.lead, trip)


@pytest.mark.parametrize(
    ("status", "trigger"),
    [
        ("dispatched", "status_dispatched"),
        ("on_the_way", "status_on_the_way"),
        ("arrived", "status_arrived"),
    ],
)
def test_a_status_change_queues_the_rows_added_on_it(status, trigger):
    row = _added(trigger)
    trip = _trip()
    set_trip_status(trip, status)
    (tp,) = _queued(row)
    assert tp.reservation == trip


def test_the_seeded_dispatched_message_stays_retired():
    MessageTemplate.objects.filter(key="status_dispatched").update(enabled=True)
    trip = _trip()
    set_trip_status(trip, "dispatched")
    assert not TouchPoint.objects.filter(kind="status_dispatched").exists()


def test_the_last_trip_done_also_queues_the_order_messages():
    row = _added("all_trips_done")
    trip = _trip()
    set_trip_status(trip, Reservation.TripStatus.DONE)
    (tp,) = _queued(row)
    assert (tp.lead, tp.reservation) == (trip.lead, None)


# --- an inline send that fails (review focus 3) ------------------------------------------


def test_inline_send_failure_does_not_break_the_save(
    client, live, django_capture_on_commit_callbacks
):
    """Dispatched 3 hours out with the driver on file: the driver message is due now and
    goes out inline. Podium and our mail refusing it never reach the dispatcher: the status
    is saved, the TouchPoint is FAILED with Podium's error (the cron does not retry FAILED
    rows)."""
    client.force_login(UserFactory())
    trip = _trip(hours=3)
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, **DRIVER)

    with (
        patch(SEND, side_effect=PodiumAPIError("Podium said no")),
        patch("apps.notifications.email.deliver_html_email", return_value=None),
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = client.post(
            reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    trip.refresh_from_db()
    assert trip.trip_status == Reservation.TripStatus.DISPATCHED
    tp = TouchPoint.objects.get(reservation=trip, kind="driver_released")
    assert tp.status == S.FAILED
    assert "Podium said no" in tp.error


# --- the cut-over: old-engine thank-yous queued on wedding orders ------------------------


def _migration():
    from importlib import import_module

    return import_module("apps.messaging.migrations.0024_queued_wedding_thank_yous")


def _queued_thank_you(lead, *, message="thank_you", status=S.SCHEDULED) -> TouchPoint:
    row = MessageTemplate.objects.get(key=message) if message else None
    return TouchPoint.objects.create(
        lead=lead, kind="thank_you", message=row, status=status, scheduled_for=timezone.now()
    )


def _repoint():
    from django.apps import apps

    _migration().repoint(apps, None)


def _wedding_row():
    return MessageTemplate.objects.get(key="thank_you_wedding")


@pytest.mark.parametrize("message", ["thank_you", None], ids=["seeded-pointer", "no-pointer"])
def test_a_queued_thank_you_on_a_named_wedding_order_sends_the_wedding_words(message):
    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Morgan & Okafor Wedding")
    tp = _queued_thank_you(lead, message=message)
    _repoint()
    tp.refresh_from_db()
    assert (tp.message, tp.kind, tp.status) == (_wedding_row(), "thank_you", S.SCHEDULED)


@pytest.mark.parametrize("legacy", [False, True], ids=["wedding-type", "legacy-leg"])
def test_a_queued_thank_you_on_an_order_with_a_wedding_trip_is_repointed(legacy):
    from apps.leads.factories import ServiceTypeFactory
    from apps.public.services import WEDDING_SERVICE_NAME

    lead = LeadFactory(status=Lead.Status.BOOKED)
    if legacy:
        ReservationFactory(lead=lead, source_leg_id="leg-1")
    else:
        ReservationFactory(lead=lead, service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME))
    tp = _queued_thank_you(lead)
    _repoint()
    tp.refresh_from_db()
    assert tp.message == _wedding_row()


def test_a_cancelled_wedding_trip_does_not_make_a_wedding_order():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, source_leg_id="leg-1", trip_status="cancelled")
    ReservationFactory(lead=lead)
    tp = _queued_thank_you(lead)
    _repoint()
    tp.refresh_from_db()
    assert tp.message.key == "thank_you"


def test_plain_orders_and_sent_thank_yous_are_left_alone():
    plain = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=plain)
    queued = _queued_thank_you(plain)
    wedding = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Morgan & Okafor Wedding")
    sent = _queued_thank_you(wedding, status=S.SENT)
    _repoint()
    queued.refresh_from_db(), sent.refresh_from_db()
    assert queued.message.key == "thank_you"
    assert sent.message.key == "thank_you"


def test_nothing_is_repointed_while_the_wedding_thank_you_is_off():
    MessageTemplate.objects.filter(key="thank_you_wedding").update(enabled=False)
    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Morgan & Okafor Wedding")
    tp = _queued_thank_you(lead)
    _repoint()
    tp.refresh_from_db()
    assert tp.message.key == "thank_you"


def test_a_repointed_thank_you_sends_the_wedding_words(live):
    from apps.messaging import sender

    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Morgan & Okafor Wedding")
    lead.contact.phone = "+15715550001"
    lead.contact.save()
    tp = _queued_thank_you(lead)
    _repoint()
    with patch(SEND, return_value={"uid": "m"}) as send:
        sender.run_touchpoints()
    tp.refresh_from_db()
    assert tp.status == S.SENT
    bodies = [c.kwargs["body"] for c in send.call_args_list]
    assert bodies and all("Congratulations on the wedding" in b for b in bodies)


# --- a whole order cancelled and refunded --------------------------------------------------


def test_cancelling_an_order_tells_each_live_trip_once(client, live):
    """Only the trips still live are news; one already cancelled was told then."""
    from apps.accounts.models import User
    from apps.dispatch.selectors import CANCELLED_STATUSES

    row = _added("trip_cancelled")
    trip = _trip()
    gone = ReservationFactory(lead=trip.lead, trip_status=Reservation.TripStatus.CANCELLED)
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=Decimal("1000.00"))
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch(SEND, return_value={"uid": "m"}) as send:
        response = client.post(
            reverse("order_cancel_refund", args=[plan.lead_id]), {"reason": "Customer cancelled"}
        )
    assert response.status_code == 302
    trip.refresh_from_db()
    assert trip.trip_status in CANCELLED_STATUSES
    assert Lead.objects.get(pk=trip.lead_id).status == Lead.Status.LOST
    (tp,) = _queued(row)
    assert tp.reservation == trip
    assert not _queued(row, reservation=gone).exists()
    send.assert_not_called()  # due now, but sent on commit (the test never commits)


def test_a_cancelled_order_s_trip_message_goes_out(live):
    from apps.messaging import sender

    row = _added("trip_cancelled")
    trip = _trip()
    payments.cancel_booked_order(trip.lead)
    (tp,) = _queued(row)
    with patch(SEND, return_value={"uid": "m"}) as send:
        assert sender.deliver(tp) is True
    send.assert_called_once()
    tp.refresh_from_db()
    assert tp.status == S.SENT
