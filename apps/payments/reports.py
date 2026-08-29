"""Internal reports (staff email) — read-only queries over orders and plans."""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Min, Q
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import Lead

from . import ledger
from .models import PaymentPlan


def unpaid_deposit_rows(today: date | None = None) -> list[dict]:
    """Booked orders whose deposit isn't paid and whose earliest pickup is inside the
    balance window (`today + BALANCE_CHARGE_DAYS_BEFORE`), soonest/overdue first.

    A booked lead with no plan at all counts as unpaid. `collected` comes from the ledger,
    not the plan flags, so a partial card charge shows what actually arrived.
    """
    today = today or timezone.localdate()
    cutoff = today + timedelta(days=settings.BALANCE_CHARGE_DAYS_BEFORE)
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    unpaid = ~Q(payment__deposit_status=PaymentPlan.DepositStatus.PAID)
    leads = (
        Lead.objects.filter(status=Lead.Status.BOOKED)
        .filter(Q(payment__isnull=True) | unpaid)
        .annotate(earliest=Min("reservations__pickup_date"))
        .filter(earliest__isnull=False, earliest__lte=cutoff)
        .select_related("contact", "payment")
        .prefetch_related("reservations")
        .order_by("earliest", "pk")
    )
    rows = []
    for lead in leads:
        plan = getattr(lead, "payment", None)
        rows.append(
            {
                "lead": lead,
                "quote_no": lead.quote_no,
                "customer": lead.contact.name,
                "email": lead.contact.email or "",
                "phone": lead.contact.phone or "",
                "trips": lead.reservations.count(),
                "earliest_pickup": lead.earliest,
                "days_out": (lead.earliest - today).days,
                "overdue_days": max(0, (today - lead.earliest).days),
                "total": plan.quote_total if plan and plan.quote_total else lead.quote_total,
                "collected": ledger.order_balances(lead)["collected"],
                "url": f"{base}{reverse('lead_detail', args=[lead.pk])}",
            }
        )
    return rows
