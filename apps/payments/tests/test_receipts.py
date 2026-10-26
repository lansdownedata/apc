"""APC-103 — one receipt email per trip charge and per trip refund (spec 2026-10-25 §4.5)."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger, receipts, services, tasks, trip_money
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    settings.OUTBOUND_ENABLED = True
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def agent():
    return UserFactory(can_manage_payments=True)


_N = iter(range(1, 10_000))


def _trip(email=None, **kw):
    email = email or f"p{next(_N)}@ex.com"
    lead = LeadFactory(
        status=Lead.Status.BOOKED, contact=ContactFactory(name="Priya Okafor", email=email)
    )
    trip = ReservationFactory(
        lead=lead,
        trip_status=S.DONE,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(10, 0),
        pickup_timezone="America/Los_Angeles",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _paid(trip):
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
        card_brand="visa",
        card_last4="4242",
    )
    ledger.post_capture(
        lead=trip.lead,
        amount=trip.line_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"paid-{trip.pk}",
    )
    return plan


def _intent(pi="pi_trip", amount=20000, last4="4242"):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        client_secret=f"{pi}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4=last4)),
    )


def _charge(trip, amount=Decimal("200.00")):
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent()),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent()),
    ):
        return trip_money.charge_trip(trip, amount)


def _overtime(trip, hours="1"):
    Reservation.objects.filter(pk=trip.pk).update(overtime_hours=Decimal(hours))
    trip.refresh_from_db()


# --- sending ------------------------------------------------------------------------------


def test_a_card_charge_sends_one_receipt_naming_the_card(django_capture_on_commit_callbacks):
    trip = _trip(email="p@ex.com")
    _paid(trip)
    _overtime(trip)
    with django_capture_on_commit_callbacks(execute=True):
        charge = _charge(trip)

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["p@ex.com"]
    assert f"R-{charge.pk}" in message.subject
    html = message.alternatives[0][0]
    assert "••4242" in html and "$200.00" in html
    charge.refresh_from_db()
    assert charge.receipt_sent_at is not None
    assert receipts.send_receipt(charge.pk) is False  # never twice
    assert len(mail.outbox) == 1


def test_the_receipt_is_queued_only_once_the_card_is_on_the_charge():
    """Outside a transaction on_commit runs at once, so `record_payment` must queue the
    receipt after it stores the card, or the receipt goes out without it."""
    trip = _trip()
    _paid(trip)
    _overtime(trip)
    seen = []
    with patch.object(
        receipts, "queue", side_effect=lambda c: seen.append(Charge.objects.get(pk=c.pk).card_last4)
    ):
        _charge(trip)

    assert seen == ["4242"]


def test_nothing_goes_out_while_outbound_is_off(settings, django_capture_on_commit_callbacks):
    settings.OUTBOUND_ENABLED = False
    trip = _trip()
    _paid(trip)
    _overtime(trip)
    with django_capture_on_commit_callbacks(execute=True):
        charge = _charge(trip)

    charge.refresh_from_db()
    assert mail.outbox == []
    assert charge.receipt_due_at is not None and charge.receipt_sent_at is None


def test_a_send_that_raises_never_breaks_the_charge(django_capture_on_commit_callbacks):
    trip = _trip()
    _paid(trip)
    _overtime(trip)
    with patch.object(receipts, "deliver_html_email", side_effect=RuntimeError("down")):
        with django_capture_on_commit_callbacks(execute=True):
            charge = _charge(trip)

    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED and charge.receipt_sent_at is None


def test_the_receipt_never_shows_cost(django_capture_on_commit_callbacks):
    trip = _trip(affiliate_cost=Decimal("377.77"))
    _paid(trip)
    _overtime(trip)
    with django_capture_on_commit_callbacks(execute=True):
        _charge(trip)

    message = mail.outbox[0]
    for body in (message.body, message.alternatives[0][0]):
        assert "377.77" not in body
        assert str(trip.vendor_pay) not in body


def test_the_billing_contact_without_email_falls_back_to_the_booking_contact(
    django_capture_on_commit_callbacks,
):
    trip = _trip(email="p@ex.com")
    trip.lead.billing_contact = ContactFactory(email="")
    trip.lead.save(update_fields=["billing_contact"])
    _paid(trip)
    _overtime(trip)
    with django_capture_on_commit_callbacks(execute=True):
        _charge(trip)

    assert mail.outbox[0].to == ["p@ex.com"]


# --- what it says -------------------------------------------------------------------------


def test_a_difference_charge_lists_the_trip_less_what_was_paid():
    trip = _trip()
    _paid(trip)
    _overtime(trip)
    charge = _charge(trip)

    lines = dict(charge.receipt_lines)
    assert lines["Overtime (1 hr)"] == "200.00"
    assert lines["Already paid"] == "-600.00"
    assert sum(Decimal(a) for _, a in charge.receipt_lines) == Decimal("200.00")


def test_an_added_charge_partly_covered_by_credit_says_so(agent):
    trip = _trip()
    _paid(trip)
    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("80"))
    processing.process(trip, user=agent, expected="-80.00", collect="keep", reason="Held")
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent(amount=2000)),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent(amount=2000)),
    ):
        charge = processing.add_charge(trip, user=agent, amount="100", label="Cleaning")

    assert charge.receipt_lines == [["Cleaning", "100.00"], ["Credit applied", "-80.00"]]


def test_receipt_dates_are_in_the_trips_zone():
    trip = _trip()
    _paid(trip)
    _overtime(trip)
    charge = _charge(trip)
    Charge.objects.filter(pk=charge.pk).update(
        receipt_due_at=datetime(2026, 10, 20, 3, 30, tzinfo=UTC)  # Oct 19 in Los Angeles
    )
    charge.refresh_from_db()

    assert receipts.receipt_context(charge)["when"] == "Oct 19, 2026"


# --- refunds ------------------------------------------------------------------------------


def test_a_refund_split_over_two_charges_sends_one_receipt(django_capture_on_commit_callbacks):
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    _charge(trip)  # a 200 trip charge, plus the 600 prepaid
    mail.outbox.clear()
    refunds = [MagicMock(id="re_1"), MagicMock(id="re_2")]
    with patch.object(stripe.Refund, "create", side_effect=refunds) as create:
        with django_capture_on_commit_callbacks(execute=True):
            trip_money.refund_trip(trip, Decimal("300"), reason="Late pickup")

    assert create.call_count == 2
    parent, part = plan.charges.filter(kind=Charge.Kind.REFUND).order_by("pk")
    assert parent.receipt_due_at is not None and part.receipt_due_at is None
    assert part.receipt_parent_id == parent.pk
    assert (part.card_last4, parent.card_last4) == ("4242", "4242")
    assert len(mail.outbox) == 1
    html = mail.outbox[0].alternatives[0][0]
    assert "$300.00" in html and "Late pickup" in html and f"R-{parent.pk}" in html


def test_an_off_card_refund_receipt_names_the_check(django_capture_on_commit_callbacks):
    trip = _trip()
    _paid(trip)
    with django_capture_on_commit_callbacks(execute=True):
        trip_money.refund_trip(
            trip, Decimal("40"), reason="Goodwill", method="check", reference="1042"
        )

    text = mail.outbox[0].body
    assert "check" in text.lower() and "1042" in text and "$40.00" in text


def test_an_order_console_refund_sends_no_receipt(django_capture_on_commit_callbacks):
    trip = _trip()
    plan = _paid(trip)
    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_x")):
        with django_capture_on_commit_callbacks(execute=True):
            services.refund_payment(plan, Decimal("50"))

    row = plan.charges.get(kind=Charge.Kind.REFUND)
    assert row.receipt_due_at is None and mail.outbox == []
    assert tasks.retry_receipts(now=timezone.now() + timedelta(hours=1)) == 0


def test_a_refund_already_recorded_by_a_concurrent_call_stops_the_walk():
    """The IntegrityError path: another request recorded this Stripe refund first. That
    refund is done; this call must not go on to refund the next charge too."""
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    _charge(trip)
    plan.charges.create(
        kind=Charge.Kind.REFUND,
        amount=Decimal("200"),
        status=Charge.Status.SUCCEEDED,
        idempotency_key="refund-re_dup",
    )
    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_dup")) as create:
        services.refund_payment(plan, Decimal("300"), reservation=trip)

    assert create.call_count == 1


# --- pay links and the retry --------------------------------------------------------------


def test_a_pay_link_paid_later_sends_once_however_often_it_reconciles(
    django_capture_on_commit_callbacks,
):
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent("pi_link")),
        patch.object(stripe.Customer, "create", return_value=MagicMock(id="cus_1")),
    ):
        trip_money.open_trip_intent(trip, Decimal("200"))
    with patch.object(stripe.PaymentIntent, "retrieve", return_value=_intent("pi_link")):
        with django_capture_on_commit_callbacks(execute=True):
            services.record_payment(plan, "pi_link", kind=Charge.Kind.TRIP)  # inline confirm
            services.record_payment(plan, "pi_link", kind=Charge.Kind.TRIP)  # the webhook

    assert len(mail.outbox) == 1
    assert "Thank you for your payment" in mail.outbox[0].body


def _due(trip, ago):
    plan = _paid(trip)
    return plan.charges.create(
        kind=Charge.Kind.TRIP,
        reservation=trip,
        amount=Decimal("10"),
        status=Charge.Status.SUCCEEDED,
        idempotency_key=f"t-{trip.pk}",
        receipt_due_at=timezone.now() - ago,
        receipt_lines=[["Parking", "10.00"]],
    )


def test_the_retry_sends_what_was_missed_and_leaves_young_and_stale_rows():
    _due(_trip(), timedelta(minutes=5))
    missed = _due(_trip(), timedelta(hours=1))
    _due(_trip(), timedelta(days=8))

    assert tasks.retry_receipts() == 1
    missed.refresh_from_db()
    assert missed.receipt_sent_at is not None and len(mail.outbox) == 1


def test_the_retry_does_nothing_while_outbound_is_off(settings):
    settings.OUTBOUND_ENABLED = False
    _due(_trip(), timedelta(hours=1))

    assert tasks.retry_receipts() == 0


def test_reconcile_payments_runs_the_receipt_retry():
    with patch.object(tasks, "retry_receipts", return_value=2) as retry:
        tasks.reconcile_payments()

    retry.assert_called_once()


# --- staff --------------------------------------------------------------------------------


def test_payments_staff_see_the_receipt(client, agent):
    charge = _due(_trip(), timedelta(hours=1))
    client.force_login(agent)

    resp = client.get(reverse("order_receipt", args=[charge.pk]))

    assert resp.status_code == 200
    body = resp.content.decode()
    assert f"R-{charge.pk}" in body and "Not sent yet" in body


def test_a_row_that_is_not_a_receipt_has_no_page(client, agent):
    trip = _trip()
    plan = _paid(trip)
    client.force_login(agent)

    resp = client.get(reverse("order_receipt", args=[plan.charges.get().pk]))

    assert resp.status_code == 404


def test_the_receipt_page_needs_payments_access(client):
    charge = _due(_trip(), timedelta(hours=1))
    client.force_login(UserFactory())

    assert client.get(reverse("order_receipt", args=[charge.pk])).status_code == 403


def test_processing_links_each_receipt(client, agent):
    charge = _due(_trip(), timedelta(hours=1))
    client.force_login(agent)

    body = client.get(reverse("processing"), {"trip": charge.reservation_id}).content.decode()

    assert reverse("order_receipt", args=[charge.pk]) in body


def test_a_part_never_becomes_a_receipt_of_its_own():
    """Parts carry no marker: even if the head row went away they would send nothing."""
    trip = _trip()
    plan = _paid(trip)
    parent = plan.charges.create(
        kind=Charge.Kind.REFUND,
        amount=1,
        status="succeeded",
        idempotency_key="p",
        reservation=trip,
        receipt_due_at=timezone.now(),
    )
    part = plan.charges.create(
        kind=Charge.Kind.REFUND,
        amount=1,
        status="succeeded",
        idempotency_key="c",
        reservation=trip,
        receipt_parent=parent,
    )
    Charge.objects.filter(pk=parent.pk).delete()

    part.refresh_from_db()
    assert part.receipt_parent_id is None and not receipts.is_receipt(part)


# --- branch-review fixes -------------------------------------------------------------------


def test_the_staff_page_keeps_the_email_inside_its_frame(client, agent):
    charge = _due(_trip(), timedelta(hours=1))
    client.force_login(agent)

    body = client.get(reverse("order_receipt", args=[charge.pk])).content.decode()

    assert 'srcdoc="&lt;' in body.replace("\n", "") or 'srcdoc="&lt;' in body
    assert '<meta charset="utf-8">' not in body.split("srcdoc=")[1]


def test_rows_that_cannot_send_do_not_starve_the_retry():
    for _ in range(tasks.RECEIPT_BATCH + 1):
        row = _due(_trip(), timedelta(hours=1))
        contact = row.reservation.lead.contact
        contact.email = ""
        contact.save(update_fields=["email"])
    sendable = _due(_trip(), timedelta(hours=3))  # older than every blocked row

    tasks.retry_receipts()
    tasks.retry_receipts()

    sendable.refresh_from_db()
    assert sendable.receipt_sent_at is not None


def test_a_reused_charge_row_says_what_it_is_for_now():
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    with (
        patch.object(stripe.PaymentIntent, "create", return_value=_intent("pi_link")),
        patch.object(stripe.Customer, "create", return_value=MagicMock(id="cus_1")),
    ):
        trip_money.open_trip_intent(
            trip, Decimal("200"), note="Cleaning", lines=[["Cleaning", 200]]
        )
        trip_money.open_trip_intent(trip, Decimal("200"))

    row = plan.charges.get(kind=Charge.Kind.TRIP)
    assert row.note == "" and receipts.receipt_context(row)["explanation"] == "overtime"


def test_a_refund_cut_short_still_queues_the_part_that_moved():
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    _charge(trip)
    with (
        patch.object(
            stripe.Refund,
            "create",
            side_effect=[MagicMock(id="re_1"), stripe.error.APIError("down")],
        ),
        patch.object(receipts, "queue") as queue,
    ):
        with pytest.raises(stripe.error.APIError):
            trip_money.refund_trip(trip, Decimal("300"), reason="Late pickup")

    queue.assert_called_once()
    assert queue.call_args.args[0] == plan.charges.get(kind=Charge.Kind.REFUND)


def test_the_losing_double_click_refunds_and_credits_nothing():
    trip = _trip()
    plan = _paid(trip)
    _overtime(trip)
    _charge(trip)
    plan.charges.create(
        kind=Charge.Kind.REFUND,
        amount=Decimal("200"),
        status=Charge.Status.SUCCEEDED,
        idempotency_key="refund-re_dup",
    )
    with patch.object(stripe.Refund, "create", return_value=MagicMock(id="re_dup")):
        refunded = trip_money.refund_trip(trip, Decimal("100"), reason="Twice")

    assert refunded == Decimal("0.00")
    assert not trip.extras.filter(kind="credit").exists()


def test_other_and_nameless_copy_reads_well():
    trip = _trip()
    _paid(trip)
    contact = trip.lead.contact
    contact.name = ""
    contact.save(update_fields=["name"])
    trip_money.refund_trip(trip, Decimal("40"), reason="Goodwill", method="other")
    row = Charge.objects.get(kind=Charge.Kind.REFUND)

    from django.template.loader import render_to_string

    text = render_to_string("email/trip_receipt.txt", receipts.receipt_context(row))
    assert "Hi ," not in text and "by other" not in text
