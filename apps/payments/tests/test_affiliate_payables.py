"""APC-61 — affiliate payables: one per confirmed farmed-out trip once its order enters
review. We record and track; no money moves to the affiliate from here.

Approving accrues the invoice amount (D Vendor Cost / C Vendor Payable); marking it paid
clears it against Affiliate disbursements — never Cash, which is what an order's
"Collected" reads (Moe, 2026-09-27)."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.core.choices import Account
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger, payables
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import AffiliatePayable, JournalEntry
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def accountant():
    return UserFactory(can_manage_payments=True)


def _trip(
    lead=None, *, status=Assignment.Status.CONFIRMED, in_house=False, covered=True, vendor=None
):
    """A 3h trip at $200/h on the 70% factor (payout $420) that ran yesterday."""
    lead = lead or LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(18, 0),
        pickup_timezone="America/New_York",
        rate=Decimal("200"),
        hours=Decimal("3"),
        cost_ratio_pct=Decimal("70"),
    )
    if in_house:
        AssignmentFactory(reservation=trip, in_house=True)
    elif covered:
        AssignmentFactory(
            reservation=trip, status=status, payout=Decimal("420"), vendor=vendor or VendorFactory()
        )
    return trip


def _entered(*trips):
    run_tasks()
    return trips


def _payable(trip) -> AffiliatePayable:
    return AffiliatePayable.objects.get(assignment__reservation=trip)


def _bill(trip, minutes):
    start = trip.pickup_at
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=minutes),
        billable_overtime_minutes=minutes,
    )
    payables.refresh_for_trip(trip)
    return review


def _invoiced(trip, amount="420.00", number="INV-1"):
    payable = _payable(trip)
    return payables.record_invoice(
        payable, user=UserFactory(can_manage_payments=True), number=number, amount=Decimal(amount)
    )


# --- one payable per confirmed farmed-out assignment ---------------------------------


def test_entering_review_creates_one_payable_per_farmed_out_trip():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    a, b = _entered(_trip(lead), _trip(lead))

    assert AffiliatePayable.objects.count() == 2
    assert _payable(a).expected_amount == Decimal("420.00")
    assert _payable(a).status == AffiliatePayable.Status.DRAFT

    run_tasks()
    payables.ensure_payables(lead)
    assert AffiliatePayable.objects.count() == 2  # idempotent


def test_no_payable_for_in_house_uncovered_or_unconfirmed_trips():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    _entered(
        _trip(lead, in_house=True),
        _trip(lead, covered=False),
        _trip(lead, status=Assignment.Status.OFFERED),
    )

    assert AffiliatePayable.objects.count() == 0


def test_no_payable_before_the_order_enters_review():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead, pickup_date=timezone.localdate() + timedelta(days=5), pickup_time=time(9, 0)
    )
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    run_tasks()

    assert AffiliatePayable.objects.count() == 0


# --- expected: recomputed until approved, then frozen --------------------------------


def test_expected_follows_the_billed_overtime_until_approval(accountant):
    (trip,) = _entered(_trip())
    _bill(trip, 30)  # $100 billed × 70% = $70
    assert _payable(trip).expected_amount == Decimal("490.00")

    _invoiced(trip, "490.00")
    payables.approve(_payable(trip), user=accountant)
    _bill(trip, 60)

    assert _payable(trip).expected_amount == Decimal("490.00")


def test_variance_is_invoice_minus_expected():
    (trip,) = _entered(_trip())
    assert _payable(trip).variance is None

    payable = _invoiced(trip, "450.00")

    assert payable.variance == Decimal("30.00")


def test_the_stage_follows_the_invoice_and_status(accountant):
    (trip,) = _entered(_trip())
    assert _payable(trip).stage == "invoice_missing"

    _invoiced(trip)
    assert _payable(trip).stage == "awaiting_approval"

    payables.approve(_payable(trip), user=accountant)
    assert _payable(trip).stage == "approved"

    payables.mark_paid(_payable(trip), user=accountant, method="ach", reference="T-88")
    assert _payable(trip).stage == "paid"


def test_an_invoice_file_is_kept(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    (trip,) = _entered(_trip())
    payable = payables.record_invoice(
        _payable(trip),
        user=UserFactory(can_manage_payments=True),
        number="INV-9",
        amount=Decimal("420"),
        file=SimpleUploadedFile("inv.pdf", b"%PDF-1.4", content_type="application/pdf"),
    )

    assert payable.invoice_file.name.endswith(".pdf")
    assert payable.invoice_received_at is not None


# --- approve -------------------------------------------------------------------------


def test_approving_needs_an_invoice(accountant):
    (trip,) = _entered(_trip())

    with pytest.raises(payables.PayableError, match="invoice"):
        payables.approve(_payable(trip), user=accountant)


def test_approving_with_a_variance_needs_a_note(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip, "450.00")

    with pytest.raises(payables.PayableError, match="note"):
        payables.approve(_payable(trip), user=accountant)

    payable = payables.approve(_payable(trip), user=accountant, note="Waited 30 min at FBO")
    assert payable.status == AffiliatePayable.Status.APPROVED
    assert payable.approval_note == "Waited 30 min at FBO"


def test_approving_needs_payments_access():
    (trip,) = _entered(_trip())
    _invoiced(trip)

    with pytest.raises(payables.PayableError, match="payments"):
        payables.approve(_payable(trip), user=UserFactory())


def test_approving_accrues_the_invoice_amount_and_balances(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip, "450.00")

    payable = payables.approve(_payable(trip), user=accountant, note="Extra stop")

    entry = JournalEntry.objects.get(kind=JournalEntry.Kind.PAYABLE_ACCRUED)
    assert entry.is_balanced
    assert entry.reservation_id == trip.pk
    lines = {(line.account, line.debit, line.credit) for line in entry.lines.all()}
    assert lines == {
        (Account.VENDOR_COST, Decimal("450.00"), Decimal("0.00")),
        (Account.VENDOR_PAYABLE, Decimal("0.00"), Decimal("450.00")),
    }
    assert payable.approved_by == accountant and payable.approved_at is not None


def test_approving_twice_is_refused_and_posts_once(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip)
    payables.approve(_payable(trip), user=accountant)

    with pytest.raises(payables.PayableError):
        payables.approve(_payable(trip), user=accountant)
    assert JournalEntry.objects.filter(kind=JournalEntry.Kind.PAYABLE_ACCRUED).count() == 1


def test_an_invoice_cannot_change_once_approved(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip)
    payables.approve(_payable(trip), user=accountant)

    with pytest.raises(payables.PayableError):
        _invoiced(trip, "999.00")


def test_approving_closes_the_approval_task_and_opens_paid(accountant):
    (trip,) = _entered(_trip())
    reviews.complete_review(_bill(trip, 0), user=UserFactory())
    assert Task.objects.get(reservation=trip, kind="affiliate_payable_approved").status == "open"
    _invoiced(trip)

    payables.approve(_payable(trip), user=accountant)

    assert Task.objects.get(reservation=trip, kind="affiliate_payable_approved").status == "done"
    assert Task.objects.get(reservation=trip, kind="affiliate_paid").status == "open"


# --- mark paid -----------------------------------------------------------------------


def test_paying_needs_approval_first(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip)

    with pytest.raises(payables.PayableError, match="Approve"):
        payables.mark_paid(_payable(trip), user=accountant, method="check")


def test_paying_needs_a_known_method_and_payments_access(accountant):
    (trip,) = _entered(_trip())
    _invoiced(trip)
    payables.approve(_payable(trip), user=accountant)

    with pytest.raises(payables.PayableError):
        payables.mark_paid(_payable(trip), user=accountant, method="bitcoin")
    with pytest.raises(payables.PayableError, match="payments"):
        payables.mark_paid(_payable(trip), user=UserFactory(), method="check")


def test_paying_clears_the_payable_without_touching_collected(accountant):
    (trip,) = _entered(_trip())
    lead = trip.lead
    _invoiced(trip, "450.00")
    payables.approve(_payable(trip), user=accountant, note="Extra stop")

    payable = payables.mark_paid(_payable(trip), user=accountant, method="zelle", reference="Z1")

    entry = JournalEntry.objects.get(kind=JournalEntry.Kind.PAYABLE_PAID)
    assert entry.is_balanced
    lines = {(line.account, line.debit, line.credit) for line in entry.lines.all()}
    assert lines == {
        (Account.VENDOR_PAYABLE, Decimal("450.00"), Decimal("0.00")),
        (Account.AFFILIATE_DISBURSEMENTS, Decimal("0.00"), Decimal("450.00")),
    }
    assert ledger.account_balance(lead, Account.VENDOR_PAYABLE) == Decimal("0.00")
    assert ledger.order_balances(lead)["collected"] == Decimal("0.00")
    assert (payable.status, payable.paid_method, payable.paid_reference) == (
        AffiliatePayable.Status.PAID,
        "zelle",
        "Z1",
    )
    assert payable.paid_at is not None


def test_paying_closes_the_paid_task(accountant):
    (trip,) = _entered(_trip())
    reviews.complete_review(_bill(trip, 0), user=UserFactory())
    _invoiced(trip)
    payables.approve(_payable(trip), user=accountant)

    payables.mark_paid(_payable(trip), user=accountant, method="check", reference="1042")

    assert Task.objects.get(reservation=trip, kind="affiliate_paid").status == "done"


# --- the endpoints -------------------------------------------------------------------


def test_the_endpoints_are_gated_on_payments_access(client):
    (trip,) = _entered(_trip())
    _invoiced(trip)
    client.force_login(UserFactory())

    for name in ("payable_invoice", "payable_approve", "payable_paid"):
        resp = client.post(reverse(name, args=[_payable(trip).pk]), {})
        assert resp.status_code == 403, name


def test_the_endpoints_record_approve_and_pay(client, accountant):
    (trip,) = _entered(_trip())
    client.force_login(accountant)
    pk = _payable(trip).pk

    resp = client.post(
        reverse("payable_invoice", args=[pk]), {"invoice_number": "A-7", "invoice_amount": "$430"}
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["payable"]["variance"] == "10.00"

    resp = client.post(reverse("payable_approve", args=[pk]), {})
    assert resp.status_code == 400
    assert "note" in resp.json()["error"]

    resp = client.post(reverse("payable_approve", args=[pk]), {"note": "Parking"})
    assert resp.json()["payable"]["stage"] == "approved"

    resp = client.post(
        reverse("payable_paid", args=[pk]), {"method": "check", "reference": "#1042"}
    )
    assert resp.json()["payable"]["stage"] == "paid"


# --- where they're listed ------------------------------------------------------------


def test_the_vendor_list_filters_open_payables(accountant):
    vendor = VendorFactory()
    lead = LeadFactory(status=Lead.Status.BOOKED)
    missing, waiting, approved, paid = _entered(*(_trip(lead, vendor=vendor) for _ in range(4)))
    _invoiced(waiting)
    for t in (approved, paid):
        _invoiced(t)
        payables.approve(_payable(t), user=accountant)
    payables.mark_paid(_payable(paid), user=accountant, method="ach")

    def ids(stage=""):
        return {p.assignment.reservation_id for p in payables.vendor_payables(vendor, stage)}

    assert ids() == {missing.pk, waiting.pk, approved.pk}
    assert ids("invoice_missing") == {missing.pk}
    assert ids("awaiting_approval") == {waiting.pk}
    assert ids("approved") == {approved.pk}


def test_the_vendor_page_lists_payables_with_a_flat_query_count(client, accountant):
    client.force_login(accountant)
    vendor = VendorFactory()
    lead = LeadFactory(status=Lead.Status.BOOKED)
    _entered(_trip(lead, vendor=vendor))
    url = reverse("vendor_detail", args=[vendor.pk])

    with CaptureQueriesContext(connection) as one:
        body = client.get(url).content.decode()
    assert "Payables" in body and lead.quote_no in body

    _entered(*(_trip(LeadFactory(status=Lead.Status.BOOKED), vendor=vendor) for _ in range(4)))
    with CaptureQueriesContext(connection) as five:
        client.get(url)
    assert len(five) == len(one)


def test_final_billing_summarises_payables_by_affiliate(client, accountant):
    client.force_login(accountant)
    lead = LeadFactory(status=Lead.Status.BOOKED)
    vendor = VendorFactory(name="Capitol Coach")
    a, _b, _c = _entered(
        _trip(lead, vendor=vendor), _trip(lead, vendor=vendor), _trip(lead, in_house=True)
    )
    PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    _invoiced(a)

    body = client.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Affiliate payables" in body
    assert "Capitol Coach" in body
    assert "1 invoice missing" in body
    assert "1 awaiting approval" in body
    assert "No payable" in body


def test_final_billing_keeps_a_flat_query_count(client, accountant):
    client.force_login(accountant)

    def page(n):
        lead = LeadFactory(status=Lead.Status.BOOKED)
        _entered(*(_trip(lead) for _ in range(n)))
        PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
        with CaptureQueriesContext(connection) as ctx:
            client.get(reverse("trip_review_order", args=[lead.pk]))
        return len(ctx)

    assert page(4) == page(1)
