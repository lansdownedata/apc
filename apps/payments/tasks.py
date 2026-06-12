from celery import shared_task

from . import services
from .models import PaymentPlan


@shared_task
def charge_due_balances() -> int:
    """Charge every scheduled balance whose due date (30 days before pickup) has arrived.

    `balance_due_now` is computed from the lead's reservations, so we filter the
    SQL-able part (scheduled) and check the date in Python.
    """
    count = 0
    scheduled = PaymentPlan.objects.filter(
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED
    ).select_related("lead")
    for plan in scheduled:
        if plan.balance_due_now:
            services.charge_balance(plan)
            count += 1
    return count
