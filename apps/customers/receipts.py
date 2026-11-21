"""The trip receipt (spec §7, mockup 4): one Letter page per trip that a customer attaches to
an expense report, titled "Trip receipt" and numbered by the trip reference.

A trip has one once its final price is locked: completed and processed
(`TripReview.processed_at`), or cancelled with money kept. Its money is the trip's own
(`payments.trip_money`): the order's prepaid share allocated to it, plus the charges and
refunds made for it. Another trip's or another order's money never reaches it.

This module never imports `selectors` (which imports it for `has_receipt`). The emailed
per-charge receipts (`apps/payments/receipts.py`) are a separate thing and stay as they are.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from fpdf import FPDF
from fpdf.enums import Align, XPos, YPos

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.payments import trip_money
from apps.payments.models import Charge, PaymentPlan
from apps.payments.services import TRIP_MONEY_KINDS
from apps.planners.selectors import dispatch_phone
from apps.public.wedding import format_clock
from apps.reservations.models import Reservation, Stop

_S = Reservation.TripStatus
_CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
# The statuses a customer reads as "Cancelled": the set `selectors._CANCELLED` holds.
CANCELLED = frozenset(CANCELLED_STATUSES) - {_S.NO_SHOW}
ON_ACCOUNT = "Billed to your account"
CANCELLATION = "Cancellation charge"
# How a refund paid back outside Stripe reads (`Charge.RefundMethod`).
_REFUND_BY = {
    Charge.RefundMethod.CHECK: "Refund by check",
    Charge.RefundMethod.ACH: "Refund by ACH",
    Charge.RefundMethod.ZELLE: "Refund by Zelle",
    Charge.RefundMethod.OTHER: "Refund",
}


def _priced(trip: Reservation) -> bool:
    return Decimal(trip.rate or 0) > 0


def has_receipt(trip: Reservation, allocated: Decimal | None = None) -> bool:
    """Completed and processed, or cancelled with money kept (`money_for(trip).allocated`
    above zero; an unpriced trip has no price to put on one). `allocated` is that amount when
    the caller has it already: lists pass the batched kept amounts, and read the review
    through `visible_trips`' `select_related`, so a row costs no query."""
    if trip.trip_status == _S.DONE:
        review = getattr(trip, "review", None)
        return review is not None and review.is_processed
    if trip.trip_status in CANCELLED and _priced(trip):
        if allocated is None:
            allocated = trip_money.money_for(trip).allocated
        return allocated > 0
    return False


# --- what it says --------------------------------------------------------------------------


def _zone(trip: Reservation) -> ZoneInfo:
    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def _day(at: datetime | None, zone: ZoneInfo) -> str:
    """ "Nov 18, 2026" in the trip's zone."""
    if at is None:
        return ""
    day = timezone.localtime(at, zone).date()
    return f"{day:%b} {day.day}, {day.year}"


def _when(trip: Reservation) -> str:
    """ "Wednesday, November 18, 2026 at 6:15 AM EST", or as much as is known."""
    day = trip.pickup_date
    if day is None:
        return "Date to be confirmed"
    text = f"{day:%A, %B} {day.day}, {day.year}"
    if trip.pickup_time is None:
        return text
    return f"{text} at {format_clock(trip.pickup_time)} {trip.pickup_tz_label}"


def _stop(stop: Stop) -> str:
    """An airport by its name and code, else a place's name with its address."""
    if stop.airport_id:
        title = stop.name.strip() or stop.airport.name
        code = stop.airport.iata
        return f"{title} ({code})" if code and f"({code})" not in title else title
    name, address = stop.name.strip(), stop.address.strip()
    if name and address and name.casefold() not in address.casefold():
        return f"{name}, {address}"
    return address or name


def _stops(trip: Reservation) -> list[Stop]:
    return sorted(trip.stops.all(), key=lambda s: (s.sequence, s.pk))


def _vehicle_line(trip: Reservation, stops: list[Stop]) -> str:
    """ "Executive Sedan, 1 passenger, airport transfer" """
    people = f"{trip.passengers} passenger{'' if trip.passengers == 1 else 's'}"
    if trip.trip_type == Reservation.TripType.HOURLY:
        hours = trip.billed_hours
        unit = "hour" if hours == 1 else "hours"
        kind = f"hourly, {format(hours.normalize(), 'f')} {unit}" if hours > 0 else "hourly"
    else:
        kind = "airport transfer" if any(s.airport_id for s in stops) else "transfer"
    parts = [trip.vehicle.name] if trip.vehicle_id else []
    return ", ".join([*parts, people, kind])


def _passengers(trip: Reservation) -> str:
    """The named passengers, the lead first; never a mobile."""
    named = [p for p in trip.named_passengers.all() if p.name.strip()]
    named.sort(key=lambda p: not p.is_lead)
    return ", ".join(p.name.strip() for p in named)


def _issued(trip: Reservation) -> datetime:
    """When the final price was locked: processing, or the cancellation."""
    review = getattr(trip, "review", None)
    if trip.trip_status in CANCELLED:
        return trip.cancelled_at or (review and review.processed_at) or timezone.now()
    return (review and review.processed_at) or timezone.now()


def _card(charge: Charge, plan: PaymentPlan) -> str:
    """ "Visa ending 4242": the card that paid this charge, else the order's card on file.
    Brand and last four only."""
    brand, last4 = (charge.card_brand, charge.card_last4)
    if not last4:
        brand, last4 = plan.card_brand, plan.card_last4
    if not last4:
        return ""
    return f"{brand.strip().capitalize() or 'Card'} ending {last4}"


def _method(charge: Charge, plan: PaymentPlan) -> str:
    if charge.kind == Charge.Kind.REFUND:
        how = charge.refund_method or Charge.RefundMethod.CARD
        if how != Charge.RefundMethod.CARD:
            return _REFUND_BY[how]
        card = _card(charge, plan)
        return f"Refund to {card}" if card else "Refund"
    return _card(charge, plan) or "Card payment"


def _moved_at(charge: Charge) -> datetime:
    return charge.captured_at or charge.receipt_due_at or charge.created_at


def _payments(
    trip: Reservation, money: trip_money.TripMoney, total: Decimal, zone: ZoneInfo
) -> list[tuple[str, str, Decimal]]:
    """`(date, method, amount)` for the trip's own money, oldest first: its prepaid share as
    one row dated at the order's deposit, then each succeeded charge and refund made for the
    trip (a refund negative; one split over several card charges is one row). The rows add
    up to `money.allocated`. An on-account order is one "Billed to your account" row."""
    lead = trip.lead
    if lead.on_account:
        return [("", ON_ACCOUNT, total)]
    plan = getattr(lead, "payment", None)
    if plan is None:
        return []
    rows: list[tuple[datetime, str, Decimal]] = []
    if money.prepaid > 0:
        prepaid = plan.charges.filter(
            status=Charge.Status.SUCCEEDED, kind__in=trip_money.PREPAID_KINDS
        ).order_by("created_at", "pk")
        first = prepaid.filter(kind=Charge.Kind.DEPOSIT).first() or prepaid.first()
        if first is not None:
            rows.append((_moved_at(first), _method(first, plan), money.prepaid))
    own = list(
        Charge.objects.filter(
            reservation=trip,
            status=Charge.Status.SUCCEEDED,
            kind__in=(*TRIP_MONEY_KINDS, Charge.Kind.REFUND),
        ).order_by("created_at", "pk")
    )
    ids = {c.pk for c in own}
    heads = {c.pk: c for c in own if c.receipt_parent_id not in ids}
    amounts = {pk: Decimal(c.amount) for pk, c in heads.items()}
    for charge in own:
        if charge.pk not in heads:
            amounts[charge.receipt_parent_id] += Decimal(charge.amount)
    for pk, charge in heads.items():
        sign = -1 if charge.kind == Charge.Kind.REFUND else 1
        rows.append((_moved_at(charge), _method(charge, plan), sign * amounts[pk]))
    rows.sort(key=lambda row: row[0])
    return [(_day(at, zone), method, amount.quantize(_CENTS)) for at, method, amount in rows]


def _company() -> dict:
    """What the settings hold. There is no address setting yet, so it stays blank."""
    return {
        "name": settings.COMPANY_NAME,
        "address": "",
        "phone": dispatch_phone()["display"],
        "email": settings.COMPANY_EMAIL,
    }


def receipt_context(trip: Reservation) -> dict:
    """Everything the receipt prints, as plain values. `trip` comes from
    `selectors.trip_or_404`. Customer-facing only: never vendor pay, affiliate cost, a note
    or a Stripe id."""
    lead = trip.lead
    zone = _zone(trip)
    money = trip_money.money_for(trip)
    if trip.trip_status in CANCELLED:
        total = money.allocated
        lines = [(CANCELLATION, total)]
    else:
        total = trip.line_total
        lines = [(line.label, line.amount) for line in trip.price_lines()]
    payments = _payments(trip, money, total, zone)
    balance_due = None
    if payments and not lead.on_account:
        balance_due = (total - sum((a for _, _, a in payments), ZERO)).quantize(_CENTS)
    contact = lead.effective_billing_contact
    stops = _stops(trip)
    return {
        "number": trip.reference,
        "issued": _day(_issued(trip), zone),
        "billed_to": {
            "name": contact.name.strip(),
            "company": contact.company.name if contact.company_id else "",
            "email": contact.email or "",
        },
        "passenger": _passengers(trip),
        "reference": trip.customer_reference.strip(),
        "when": _when(trip),
        "route_full": " to ".join(text for s in stops if (text := _stop(s))),
        "vehicle_line": _vehicle_line(trip, stops),
        "lines": lines,
        "total": total,
        "payments": payments,
        "balance_due": balance_due,
        "company": _company(),
    }


# --- the PDF -------------------------------------------------------------------------------

# Built-in fonts only (no font files to ship): Helvetica for text, Times for the two
# headings, standing in for the portal's display serif. Their text is Windows-1252.
ENCODING = "windows-1252"
INK = (23, 25, 29)
MUTED = (107, 114, 124)
LINE = (229, 226, 217)
GOLD = (199, 162, 78)
MARGIN = 54  # points: three-quarters of an inch
GUTTER = 14


def pdf_text(text: str) -> str:
    """`text` as the core fonts can print it, never raising: a character outside
    Windows-1252 becomes its unaccented letter when it has one ("ā" -> "a"), else "?"."""
    out = []
    for char in text:
        try:
            char.encode(ENCODING)
            out.append(char)
        except UnicodeEncodeError:
            plain = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode()
            out.append(plain or "?")
    return "".join(out)


def _money(amount: Decimal) -> str:
    return f"-${-amount:,.2f}" if amount < 0 else f"${amount:,.2f}"


class _Receipt(FPDF):
    def __init__(self, number: str) -> None:
        super().__init__(orientation="portrait", unit="pt", format="letter")
        self.number = number
        self.core_fonts_encoding = ENCODING
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.c_margin = 0  # text sits flush with the margin and the rules under it
        # Blocks are kept whole and moved to a new page by `room`, never split by fpdf.
        self.set_auto_page_break(False)
        self.add_page()

    def header(self) -> None:
        """A page after the first says whose it is (fpdf calls this on every new page)."""
        if self.page_no() > 1:
            self.para(f"Trip receipt {self.number}, continued", self.width, 9, color=MUTED)
            self.ln(12)

    @property
    def width(self) -> float:
        return self.w - 2 * MARGIN

    def room(self, height: float) -> None:
        """Start a new page unless `height` more points fit on this one."""
        if self.get_y() + height > self.h - MARGIN:
            self.add_page()

    def style(
        self,
        size: float,
        bold: bool = False,
        color: tuple[int, int, int] = INK,
        family: str = "Helvetica",
    ) -> None:
        self.set_font(family, "B" if bold else "", size)
        self.set_text_color(*color)

    def para(
        self, text: str, width: float, size: float, *, x: float | None = None, **style: object
    ) -> None:
        """Wrapped text at `x` (the left margin by default), leaving y under it."""
        self.style(size, **style)
        self.set_x(MARGIN if x is None else x)
        self.multi_cell(
            width, size * 1.4, pdf_text(text), align=Align.L, new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    def height_of(self, text: str, width: float, size: float, **style: object) -> float:
        self.style(size, **style)
        lines = self.multi_cell(width, size * 1.4, pdf_text(text), dry_run=True, output="LINES")
        return len(lines) * size * 1.4

    def row(
        self, label: str, amount: str, *, size: float = 10.5, bold: bool = False, rule: bool = True
    ) -> None:
        """A label and its right-aligned amount, the label wrapping, a hairline under."""
        amount_w = 110
        label_w = self.width - amount_w - GUTTER
        pad = 6
        height = self.height_of(label, label_w, size, bold=bold) + 2 * pad
        self.room(height)
        top = self.get_y()
        self.set_y(top + pad)
        self.para(label, label_w, size, bold=bold)
        self.style(size, bold=bold)
        self.set_xy(MARGIN + label_w + GUTTER, top + pad)
        self.cell(amount_w, size * 1.4, pdf_text(amount), align=Align.R)
        self.set_y(top + height)
        if rule:
            self.set_draw_color(*LINE)
            self.set_line_width(0.75)
            self.line(MARGIN, self.get_y(), MARGIN + self.width, self.get_y())

    def label(self, text: str) -> None:
        self.para(text, self.width, 9, color=MUTED)


def _header(pdf: _Receipt, ctx: dict) -> None:
    company = ctx["company"]
    top = pdf.get_y()
    # The logo: a dark disc with a gold initial, then the company's name.
    radius = 12
    pdf.set_fill_color(*INK)
    pdf.ellipse(MARGIN, top, 2 * radius, 2 * radius, style="F")
    pdf.style(12, bold=True, color=GOLD, family="Times")
    pdf.set_xy(MARGIN, top + radius - 6)
    pdf.cell(2 * radius, 12, pdf_text(company["name"][:1].upper()), align=Align.C)
    pdf.style(18, bold=True, family="Times")
    pdf.set_xy(MARGIN + 2 * radius + 9, top + radius - 9)
    pdf.cell(250, 18, pdf_text(company["name"]))
    details = " · ".join(v for v in (company["address"], company["phone"], company["email"]) if v)
    pdf.set_y(top + 2 * radius + 8)
    if details:
        pdf.para(details, pdf.width * 0.58, 9, color=MUTED)
    left_bottom = pdf.get_y()

    right_w = pdf.width * 0.4
    right_x = MARGIN + pdf.width - right_w
    pdf.style(22, bold=True, family="Times")
    pdf.set_xy(right_x, top)
    pdf.cell(right_w, 24, "Trip receipt", align=Align.R)
    pdf.style(9, color=MUTED)
    pdf.set_xy(right_x, top + 30)
    pdf.multi_cell(
        right_w,
        12.6,
        pdf_text(f"{ctx['number']} · Issued {ctx['issued']}"),
        align=Align.R,
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_y(max(left_bottom, pdf.get_y()))


def _parties(pdf: _Receipt, ctx: dict) -> None:
    """Billed to, passenger and your reference, side by side; a blank one is left out."""
    billed = ctx["billed_to"]
    columns = [
        (
            "Billed to",
            [
                (billed["name"], True, INK),
                (billed["company"], False, INK),
                (billed["email"], False, MUTED),
            ],
        ),
        ("Passenger", [(ctx["passenger"], True, INK)]),
        ("Your reference", [(ctx["reference"], True, INK)]),
    ]
    columns = [(title, [p for p in parts if p[0]]) for title, parts in columns]
    columns = [(title, parts) for title, parts in columns if parts]
    width = (pdf.width - 2 * GUTTER) / 3
    top = pdf.get_y()
    bottom = top
    for i, (title, parts) in enumerate(columns):
        x = MARGIN + i * (width + GUTTER)
        pdf.set_y(top)
        pdf.para(title, width, 9, x=x, color=MUTED)
        for text, bold, color in parts:
            size = 10.5 if color == INK else 9
            pdf.para(text, width, size, x=x, bold=bold, color=color)
        bottom = max(bottom, pdf.get_y())
    pdf.set_y(bottom)


def _trip(pdf: _Receipt, ctx: dict) -> None:
    pdf.label("Trip")
    pdf.para(ctx["when"], pdf.width, 12, bold=True)
    if ctx["route_full"]:
        pdf.para(ctx["route_full"], pdf.width, 10.5)
    pdf.para(ctx["vehicle_line"], pdf.width, 9, color=MUTED)


def render_pdf(context: dict) -> bytes:
    """The receipt as a Letter PDF laid out as mockup 4. One page for any ordinary trip; a
    long list of price lines runs onto a second rather than being cut."""
    pdf = _Receipt(context["number"])
    pdf.set_title(pdf_text(f"Trip receipt {context['number']}"))
    pdf.set_author(pdf_text(context["company"]["name"]))
    _header(pdf, context)
    pdf.ln(28)
    _parties(pdf, context)
    pdf.ln(24)
    _trip(pdf, context)
    pdf.ln(14)
    for label, amount in context["lines"]:
        pdf.row(label, _money(amount))
    pdf.row("Total", _money(context["total"]), size=13, bold=True, rule=False)
    if context["payments"]:
        pdf.ln(16)
        pdf.room(60)
        pdf.label("Payments")
        for day, method, amount in context["payments"]:
            pdf.row(" · ".join(part for part in (day, method) if part), _money(amount))
        if context["balance_due"] is not None:
            pdf.row("Balance due", _money(context["balance_due"]), bold=True, rule=False)
    company = context["company"]
    thanks = f"Thank you for riding with {company['name']}."
    if company["email"]:
        thanks += f" Questions about this receipt: {company['email']}"
    pdf.ln(24)
    pdf.room(pdf.height_of(thanks, pdf.width, 9))
    pdf.para(thanks, pdf.width, 9, color=MUTED)
    return bytes(pdf.output())
