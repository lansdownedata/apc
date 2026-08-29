from django.utils import timezone

from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation

from . import ledger, services
from .models import PaymentPlan


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


def send_unpaid_deposit_report(today=None) -> int:
    """Email the office every booked order with an unpaid deposit and a trip inside the
    balance window. Nothing goes out on an empty day. Returns the number of orders listed."""
    from django.conf import settings

    from apps.notifications.email import send_html_email

    from . import reports

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
    for recipient in settings.DEPOSIT_REPORT_EMAILS:
        send_html_email(to=recipient, subject=subject, template="deposit_report", context=context)
    return count
