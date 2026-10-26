"""The customer's receipt for money moved on a finished trip (APC-103, spec 2026-10-25 §4.5).

One email per trip charge that succeeds and per trip refund, to the order's billing contact.
A row is a receipt only when `Charge.receipt_due_at` is set — processing sets it at the
moment the money moves — and it has no `receipt_parent` (a refund split over several
Stripe charges is one receipt, carried by its first row).

`send_receipt` is the only sender: under a row lock, once, never raising. The money paths
call `queue` (after commit); `tasks.retry_receipts` picks up anything that did not go.
Sends go through `deliver_html_email`, so `OUTBOUND_ENABLED` gates them.

The copy is DRAFT until the client signs it off
(docs/touchpoints/2026-10-25-APC-103-receipt-wording-for-client.md).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.notifications.email import deliver_html_email

from .models import Charge

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def is_receipt(charge: Charge) -> bool:
    """A row that carries a receipt: marked by processing, the head of its refund, and the
    money actually moved."""
    return (
        charge.receipt_due_at is not None
        and charge.receipt_parent_id is None
        and charge.reservation_id is not None
        and charge.status == Charge.Status.SUCCEEDED
        and charge.kind in (Charge.Kind.TRIP, Charge.Kind.REFUND)
    )


def queue(charge: Charge) -> None:
    """Send `charge`'s receipt once the surrounding transaction commits (at once outside
    one)."""
    pk = charge.pk
    transaction.on_commit(lambda: send_receipt(pk))


def _zone(trip) -> ZoneInfo:
    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def _route(trip) -> str:
    stops = list(trip.stops.order_by("sequence"))

    def short(stop) -> str:
        return stop.name or (stop.address or "").split(",")[0]

    if not stops:
        return ""
    return f"{short(stops[0])} → {short(stops[-1])}" if len(stops) > 1 else short(stops[0])


def _money(value: Decimal) -> str:
    """ "$1,234.00" / "−$80.00" (a true minus sign)."""
    return f"−${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def _card(row: Charge) -> str:
    return f"{row.card_brand.title()} ••{row.card_last4}" if row.card_last4 else ""


def _explanation(charge: Charge) -> str:
    """Which sentence explains the money (the template words it)."""
    if charge.kind == Charge.Kind.REFUND:
        method = charge.refund_method or Charge.RefundMethod.CARD
        return "refund_card" if method == Charge.RefundMethod.CARD else "refund_other"
    labels = [label for label, _amount in charge.receipt_lines]
    if charge.note:
        return "added"
    if any(label.startswith(("Overtime", "Wait time")) for label in labels):
        return "overtime"
    return "change"


def receipt_context(charge: Charge) -> dict:
    """Everything the receipt shows. Customer-facing only: prices and money, never vendor
    pay, affiliate cost or margin."""
    trip = charge.reservation
    lead = trip.lead
    contact = lead.effective_billing_contact
    parts = list(charge.receipt_parts.all()) if charge.kind == Charge.Kind.REFUND else []
    amount = (Decimal(charge.amount) + sum((Decimal(p.amount) for p in parts), ZERO)).quantize(
        Decimal("0.01")
    )
    is_refund = charge.kind == Charge.Kind.REFUND
    if is_refund:
        lines = [[f"Refund: {charge.note}" if charge.note else "Refund", f"{amount:.2f}"]]
    else:
        lines = [list(line) for line in charge.receipt_lines] or [["Trip charge", f"{amount:.2f}"]]
    cards = []
    for row in [charge, *parts]:
        card = _card(row)
        if card and card not in cards:
            cards.append(card)
    due = charge.receipt_due_at or charge.created_at
    hours = Decimal(trip.billed_hours or 0)
    kind = (
        f"Hourly, {hours.normalize():f} hrs"
        if trip.trip_type == trip.TripType.HOURLY
        else "Transfer"
    )
    return {
        "number": charge.receipt_number,
        "is_refund": is_refund,
        "paid_by_link": not is_refund and bool(charge.stripe_client_secret),
        "amount": f"{amount:,.2f}",
        "cards": cards,
        "method_label": (
            charge.get_refund_method_display()
            if is_refund and charge.refund_method not in ("", Charge.RefundMethod.CARD)
            else ""
        ),
        "reference": charge.reference if is_refund else "",
        "when": due.astimezone(_zone(trip)).strftime("%b %-d, %Y"),
        "trip_ref": trip.reference,
        "trip_kind": kind,
        "trip_date": trip.pickup_date.strftime("%A, %B %-d") if trip.pickup_date else "",
        "trip_date_short": trip.pickup_date.strftime("%a %b %-d") if trip.pickup_date else "",
        "route": _route(trip),
        "lines": [(label, _money(Decimal(value))) for label, value in lines],
        "explanation": _explanation(charge),
        "added_for": charge.note,
        "first_name": (contact.name or lead.contact.name or "").split(" ")[0],
        "company_name": settings.COMPANY_NAME,
        "company_phone": settings.COMPANY_PHONE,
        "company_email": settings.COMPANY_EMAIL,
    }


def recipient(charge: Charge) -> str:
    """The billing contact's email, else the booking contact's."""
    lead = charge.reservation.lead
    return (lead.effective_billing_contact.email or lead.contact.email or "").strip()


def subject(context: dict) -> str:
    what = "refund" if context["is_refund"] else "receipt"
    return f"Your {context['company_name']} {what} {context['number']}"


def send_receipt(charge_id: int) -> bool:
    """Send one receipt, once. True when it went out now. Never raises: a failure leaves it
    unsent for `tasks.retry_receipts`."""
    try:
        with transaction.atomic():
            charge = (
                Charge.objects.select_for_update()
                .select_related("reservation__lead__contact")
                .filter(pk=charge_id)
                .first()
            )
            if charge is None or charge.receipt_sent_at is not None or not is_receipt(charge):
                return False
            to = recipient(charge)
            if not to:
                logger.warning("receipt %s not sent: no email on the order", charge_id)
                return False
            context = receipt_context(charge)
            message_id = deliver_html_email(
                to=to, subject=subject(context), template="trip_receipt", context=context
            )
            if message_id is None:
                return False
            charge.receipt_sent_at = timezone.now()
            charge.receipt_message_id = message_id[:120]
            charge.save(update_fields=["receipt_sent_at", "receipt_message_id", "updated_at"])
            return True
    except Exception:  # noqa: BLE001 - a receipt must never break the money path
        logger.exception("receipt for charge %s failed", charge_id)
        return False
