import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.notifications.email import send_html_email
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation

from . import ledger, reports, services
from .models import Charge, PaymentPlan

logger = logging.getLogger(__name__)

# Old enough never to race the customer's own `complete` POST or a just-in-time webhook.
RECONCILE_MIN_AGE = timedelta(minutes=10)
# Cron endpoints are synchronous HTTP behind Heroku's 30 s router timeout — a backlog drains
# across runs rather than timing out on one.
RECONCILE_BATCH = 50


def charge_due_balances() -> int:
    """Charge every scheduled balance whose due date (30 days before pickup) has arrived.

    `balance_due_now` is computed from the lead's reservations, so we filter the
    SQL-able part (scheduled, card on file) and check the date in Python. A plan with no
    saved card — a directly booked order, or one whose link was never paid — is skipped
    rather than failed: there is nothing to charge, and the daily unpaid-deposit report is
    the nudge for those (spec 2026-08-29 §6).
    """
    count = 0
    scheduled = (
        PaymentPlan.objects.filter(balance_status=PaymentPlan.BalanceStatus.SCHEDULED)
        .exclude(stripe_payment_method_id="")
        .select_related("lead")
    )
    for plan in scheduled:
        if plan.balance_due_now:
            services.charge_balance(plan)
            count += 1
    return count


def recognize_due_revenue() -> int:
    """Recognize each earned, past-pickup, still-deferred trip's revenue (spec §5)."""
    today = timezone.localdate()
    due = Reservation.objects.filter(
        pickup_date__lt=today,
        trip_status__in=EARNED_TERMINAL_STATUSES,
        revenue_status=Reservation.RevenueStatus.DEFERRED,
    ).select_related("lead")
    count = 0
    for reservation in due:
        ledger.recognize_reservation(reservation)
        count += 1
    return count


def reconcile_open_charges(now=None) -> int:
    """Catch payments the webhook never delivered. Returns the count reconciled.

    `payment_intent.succeeded` is the only webhook success path (spec 2026-08-30 §7); when it
    goes missing — a dyno restarting mid-deploy, Stripe exhausting retries, a 500 in the
    handler, a customer closing the tab before the `complete` POST — this sweep finishes the
    job. Bounded and age-gated so it never races the normal path.
    """
    cutoff = (now or timezone.now()) - RECONCILE_MIN_AGE
    open_charges = (
        Charge.objects.filter(
            status=Charge.Status.PENDING,
            kind__in=(Charge.Kind.DEPOSIT, Charge.Kind.BALANCE),
            updated_at__lt=cutoff,
        )
        .exclude(stripe_payment_intent_id="")
        .select_related("plan__lead")
        .order_by("updated_at")[:RECONCILE_BATCH]
    )
    reconciled = 0
    for charge in open_charges:
        try:
            intent = services._stripe().PaymentIntent.retrieve(charge.stripe_payment_intent_id)
            if intent.status == "succeeded":
                services.record_payment(
                    charge.plan, charge.stripe_payment_intent_id, kind=charge.kind
                )
                reconciled += 1
            elif intent.status == "canceled":
                charge.status = Charge.Status.FAILED
                charge.save(update_fields=["status", "updated_at"])
            # any other status: still in flight — leave it PENDING for open_intent_for to reuse
        except Exception:  # noqa: BLE001 - one bad row must not kill the run
            logger.exception("reconcile-payments: charge %s failed", charge.pk)
    return reconciled


def send_unpaid_deposit_report(today=None) -> int:
    """Email the office every booked order with an unpaid deposit and a trip inside the
    balance window. Nothing goes out on an empty day. Returns the number of orders listed."""
    rows = reports.unpaid_deposit_rows(today=today)
    if not rows:
        return 0
    window = settings.BALANCE_CHARGE_DAYS_BEFORE
    count = len(rows)
    context = {
        "rows": rows,
        "today": today or timezone.localdate(),
        "window_days": window,
        "company_name": settings.COMPANY_NAME,
    }
    plural = "s" if count != 1 else ""
    subject = f"Unpaid deposits — {count} order{plural} with a trip inside {window} days"
    if not settings.DEPOSIT_REPORT_EMAILS:
        logger.warning(
            "deposit report: %d order(s) listed but DEPOSIT_REPORT_EMAILS is empty — nothing sent",
            count,
        )
    for recipient in settings.DEPOSIT_REPORT_EMAILS:
        sent = send_html_email(
            to=recipient, subject=subject, template="deposit_report", context=context
        )
        if not sent:
            logger.warning("deposit report: delivery to %s failed", recipient)
    return count
