"""Bulk writes auditlog can't hear, rewritten as saves (spec §5): they must write entries."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet

from apps.audit.models import AuditEntry
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import payables, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import AffiliatePayable, Charge
from apps.payments.tests.test_cancel_trips import _agent, _plan, _refunds, _trip
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _entries(model, **filters):
    ct = ContentType.objects.get_for_model(model)
    return AuditEntry.objects.filter(content_type=ct, **filters)


def test_cancel_one_of_two_trips_logs_on_that_trip_only():
    plan = _plan()
    t1, t2 = _trip(plan.lead), _trip(plan.lead)
    before = set(_entries(Reservation).values_list("pk", flat=True))

    with _refunds():
        services.cancel_trips(plan.lead, [t1], user=_agent())

    fresh = _entries(Reservation).exclude(pk__in=before)
    mine = fresh.filter(trip_id=t1.pk)
    assert mine.count() == 1
    assert mine.get().changes["trip_status"][1] == Reservation.TripStatus.CANCELLED
    assert not fresh.filter(trip_id=t2.pk).exists()


def test_payment_failed_webhook_logs_failed_with_stripe_source(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    charge = plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=Decimal("100.00"),
        status=Charge.Status.PENDING,
        stripe_payment_intent_id="pi_dec",
        idempotency_key="dec-1",
    )
    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_dec",
                "metadata": {"lead_id": str(plan.lead_id), "kind": "balance"},
                "last_payment_error": {"message": "Your card was declined."},
            }
        },
    }
    with patch("apps.payments.views.stripe.Webhook.construct_event", return_value=event):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )

    assert resp.status_code == 200
    entry = _entries(Charge, object_pk=str(charge.pk), action=AuditEntry.Action.UPDATE).get()
    assert entry.changes["status"][1] == Charge.Status.FAILED
    assert entry.source == "webhook: stripe"


def _farmed_trip(lead):
    trip = ReservationFactory(lead=lead)
    vendor = VendorFactory()
    AssignmentFactory(
        reservation=trip, status=Assignment.Status.CONFIRMED, payout=Decimal("420"), vendor=vendor
    )
    return trip, vendor


def test_sweep_logs_vendor_bill_created_once():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip, vendor = _farmed_trip(lead)

    first = payables.ensure_payables(lead)
    second = payables.ensure_payables(lead)

    assert [p.assignment.reservation_id for p in first] == [trip.pk]
    assert second == []
    entry = _entries(AffiliatePayable, trip_id=trip.pk).get()
    assert entry.action == AuditEntry.Action.CREATE
    assert entry.profile == f"vendor:{vendor.pk}"
    assert entry.order_id == lead.pk
    assert "expected_amount" in entry.changes


def test_ensure_payable_after_sweep_creates_no_second_row():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip, _ = _farmed_trip(lead)
    payables.ensure_payables(lead)

    payable = payables.ensure_payable(trip)

    assert AffiliatePayable.objects.filter(assignment__reservation=trip).count() == 1
    assert payable.assignment.reservation_id == trip.pk
    assert _entries(AffiliatePayable, trip_id=trip.pk).count() == 1


def test_ensure_payable_integrity_race_returns_existing():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip, _ = _farmed_trip(lead)
    rival = AffiliatePayable.objects.create(
        assignment=Assignment.objects.get(reservation=trip), expected_amount=Decimal("420")
    )
    real_filter = AffiliatePayable.objects.filter
    real_get = QuerySet.get
    seen = {"filter": 0, "get": 0}

    class _Blind:
        def first(self):
            return None

    def filter_once(*args, **kwargs):
        seen["filter"] += 1
        return _Blind() if seen["filter"] == 1 else real_filter(*args, **kwargs)

    def get_once(self, *args, **kwargs):
        # get_or_create's first read misses (the rival is not visible yet), so it INSERTs,
        # hits the unique constraint and re-reads
        if self.model is AffiliatePayable and seen["get"] == 0:
            seen["get"] += 1
            raise AffiliatePayable.DoesNotExist
        return real_get(self, *args, **kwargs)

    with (
        patch.object(AffiliatePayable.objects, "filter", side_effect=filter_once),
        patch.object(QuerySet, "get", get_once),
    ):
        payable = payables.ensure_payable(trip)

    assert seen["get"] == 1
    assert payable.pk == rival.pk
    assert AffiliatePayable.objects.filter(assignment__reservation=trip).count() == 1
    assert _entries(AffiliatePayable, trip_id=trip.pk).count() == 1
