"""The trip receipt (spec §7, mockup 4): one Letter page per trip that a customer attaches to
an expense report, titled "Trip receipt" and numbered by the trip reference.

A trip has one once its final price is locked: a trip processing settles (completed, late
cancel, no-show) once it is processed (`TripReview.processed_at`), or a plain cancellation,
which is never processed, once it keeps money. Never for $0.00. Its money is the trip's own
(`payments.trip_money`): the order's prepaid share allocated to it, plus the charges and
refunds made for it. Another trip's or another order's money never reaches it.

This module never imports `selectors`, `trip_detail` or `exports`: they import it, for
`has_receipt` and the small rules they share with the receipt (`CANCELLED`, `priced`,
`passenger_names`, `card_text`). The emailed
per-charge receipts (`apps/payments/receipts.py`) are a separate thing and stay as they are.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Min
from django.utils import timezone
from fpdf import FPDF
from fpdf.enums import Align, XPos, YPos

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.leads.models import Lead
from apps.payments import trip_money
from apps.payments.models import Charge, JournalEntry, PaymentPlan
from apps.payments.services import TRIP_MONEY_KINDS
from apps.planners.selectors import dispatch_phone
from apps.public.wedding import format_clock
from apps.reservations import processing
from apps.reservations.models import Reservation, Stop

_S = Reservation.TripStatus
_CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
# The statuses a customer reads as "Cancelled" (a no-show has its own label).
CANCELLED = frozenset(CANCELLED_STATUSES) - {_S.NO_SHOW}
# A receipt names this many passengers at most; past it, the first few and "and N more".
NAMED_PASSENGERS = 5
ON_ACCOUNT = "Billed to your account"
CANCELLATION = "Cancellation charge"
# How a refund paid back outside Stripe reads (`Charge.RefundMethod`).
_REFUND_BY = {
    Charge.RefundMethod.CHECK: "Refund by check",
    Charge.RefundMethod.ACH: "Refund by ACH",
    Charge.RefundMethod.ZELLE: "Refund by Zelle",
    Charge.RefundMethod.OTHER: "Refund",
}


# The order's stages in which the customer has been sent its price, or has accepted it.
_QUOTED_STAGES = frozenset({Lead.Status.QUOTED, Lead.Status.ENGAGED, Lead.Status.BOOKED})


def priced(trip: Reservation) -> bool:
    """The trip has a price the customer may see: it has a rate, and somebody committed to
    it. A quote sent or accepted is one (Quoted, Engaged, Booked, or Lost after the quote
    went out); so is a booking, which freezes `booked_total` on the trip.

    A draft never shows. While the order is New a rate typed in is a figure nobody has
    sent, and a request closed before its quote went out never had one. An order reopened
    to New after it booked is priced afresh too, except for its trips that already ended
    (completed, cancelled, no-show): their money was real, so their figures and receipts
    stay."""
    if Decimal(trip.rate or 0) <= 0:
        return False
    lead = trip.lead
    if lead.status in _QUOTED_STAGES:
        return True
    booked = trip.booked_total is not None
    if lead.status == Lead.Status.NEW:
        return booked and trip.trip_status in processing.TERMINAL
    return booked or lead.quote_sent_at is not None


def _processed(trip: Reservation) -> bool:
    review = getattr(trip, "review", None)
    return review is not None and review.is_processed


def awaiting_processing(trip: Reservation) -> bool:
    """A trip processing settles (completed, late cancel, no-show) that billing has not
    processed yet: its price can still change, so its receipt waits."""
    return trip.trip_status in processing.SETTLE_STATUSES and not _processed(trip)


def shows_kept(trip: Reservation) -> bool:
    """A cancelled trip whose figure is the money kept: a plain cancellation, or a late
    cancel billing has not processed yet. Once processed, a late cancel is settled to its
    price like any other trip."""
    return trip.trip_status in CANCELLED and not _processed(trip)


def customer_total(trip: Reservation, kept: Decimal | None = None) -> Decimal | None:
    """The customer's figure for this trip, wherever it shows (Trips row and foot total,
    trip page, spreadsheet, receipt): None while unpriced (`priced`: no rate, or a figure
    nobody committed to); the money kept on a cancelled trip not settled by processing
    (`kept`, its `money_for(trip).allocated`, when the caller has it: lists batch it), or
    None when it was cancelled before the order booked; otherwise the trip's price,
    `line_total`."""
    if not priced(trip):
        return None
    if shows_kept(trip):
        amount = trip_money.money_for(trip).allocated if kept is None else kept
        # Cancelled before the order ever booked: nothing was charged, so no figure at all
        # (a booked trip refunded in full reads $0.00).
        return None if trip.booked_total is None and not amount else amount
    return trip.line_total


def has_receipt(trip: Reservation, allocated: Decimal | None = None) -> bool:
    """A trip processing settles, once processed; a plain cancellation (never processed)
    as it is. Only for a figure above zero (`customer_total`): a plain cancellation that
    kept nothing has none, and there is never a receipt for $0.00. `allocated` is the kept
    amount when the caller has it already: lists pass the batched amounts and read the
    review through `visible_trips`' `select_related`, so a row costs no query."""
    status = trip.trip_status
    if status in processing.SETTLE_STATUSES:
        if not _processed(trip):
            return False
    elif status not in processing.NEVER:
        return False
    total = customer_total(trip, allocated)
    return total is not None and total > 0


def passenger_names(trip: Reservation) -> list[str]:
    """The named passengers, the lead first; never a mobile."""
    named = [p for p in trip.named_passengers.all() if p.name.strip()]
    named.sort(key=lambda p: not p.is_lead)
    return [p.name.strip() for p in named]


# Stripe's `card.brand` values, as the customer knows the card.
_CARD_BRANDS = {
    "amex": "American Express",
    "american express": "American Express",
    "cartes_bancaires": "Cartes Bancaires",
    "diners": "Diners Club",
    "diners club": "Diners Club",
    "discover": "Discover",
    "eftpos_au": "Eftpos Australia",
    "jcb": "JCB",
    "mastercard": "Mastercard",
    "unionpay": "UnionPay",
    "visa": "Visa",
}


def card_text(brand: str, last4: str) -> str:
    """ "Visa ending 4242"; blank without a last four. Brand and last four only. The brand
    reads as its own name (`_CARD_BRANDS`); one Stripe adds later reads as its words
    ("some_brand" -> "Some Brand"), and none, or Stripe's "unknown", as "Card". The one
    rule for the receipt and the spreadsheet's payment method."""
    if not last4:
        return ""
    key = brand.strip().lower()
    name = _CARD_BRANDS.get(key)
    if name is None:
        name = "" if key == "unknown" else key.replace("_", " ").title()
    return f"{name or 'Card'} ending {last4}"


def posted_at(charge_ids: Iterable[int]) -> dict[int, datetime]:
    """{charge id: when its money was first posted to the books}: the moment a payment was
    recorded, for a charge that carries no time of its own (a deposit or balance paid
    outright has no hold or capture time, and `updated_at` moves whenever the row is
    touched). A charge with no ledger entry is absent. One query."""
    rows = (
        JournalEntry.objects.filter(charge_id__in=list(charge_ids))
        .values("charge_id")
        .annotate(at=Min("posted_at"))
        .order_by()
    )
    return {row["charge_id"]: row["at"] for row in rows}


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
    """Every named passenger up to `NAMED_PASSENGERS`; past that the first few and how
    many more ("A, B, C, D and 52 more"), so a coach's list never crowds the page."""
    names = passenger_names(trip)
    if len(names) <= NAMED_PASSENGERS:
        return ", ".join(names)
    shown = names[: NAMED_PASSENGERS - 1]
    return f"{', '.join(shown)} and {len(names) - len(shown)} more"


def _issued(trip: Reservation) -> datetime:
    """When the final price was locked: processing, or the cancellation."""
    review = getattr(trip, "review", None)
    if trip.trip_status in processing.NEVER:
        return trip.cancelled_at or timezone.now()
    return (review and review.processed_at) or timezone.now()


CARD_PAYMENT = "Card payment"


def _method(charge: Charge) -> str:
    """How this charge's money moved, from the charge's own record. Only the card kept on
    the charge when it was paid is ever named: the order's card on file is the one the next
    charge will use and may never have paid this one, so a charge with none reads "Card
    payment" (a refund, "Refund to card")."""
    card = card_text(charge.card_brand, charge.card_last4)
    if charge.kind == Charge.Kind.REFUND:
        how = charge.refund_method or Charge.RefundMethod.CARD
        if how != Charge.RefundMethod.CARD:
            return _REFUND_BY[how]
        return f"Refund to {card}" if card else "Refund to card"
    return card or CARD_PAYMENT


def _moved_at(charge: Charge, posted: dict[int, datetime]) -> datetime:
    """When the money moved: the capture, else the moment processing moved it
    (`receipt_due_at`), else when it was posted to the books (`posted_at`), and only
    failing all of those when the row was made."""
    return charge.captured_at or charge.receipt_due_at or posted.get(charge.pk) or charge.created_at


def _prepaid_rows(plan: PaymentPlan, prepaid: Decimal) -> list[tuple[Charge, str, Decimal]]:
    """`(charge whose date it takes, method, amount)` for the trip's share of what the
    order prepaid, adding up to `prepaid`.

    When the share is everything the order's deposit, balance and final-bill charges
    collected (an order of one trip, as most are), each charge is its own row: what the
    card statement shows. Otherwise the share is part of a pool and no one charge is it:
    one row, dated at the order's deposit, naming a card only when every charge in the pool
    was paid by that one card."""
    if prepaid <= 0:
        return []
    charges = list(
        plan.charges.filter(
            status=Charge.Status.SUCCEEDED, kind__in=trip_money.PREPAID_KINDS
        ).order_by("created_at", "pk")
    )
    if not charges:
        return []
    if sum((Decimal(c.amount) for c in charges), ZERO) == prepaid:
        return [(c, _method(c), Decimal(c.amount)) for c in charges]
    first = next((c for c in charges if c.kind == Charge.Kind.DEPOSIT), charges[0])
    methods = {_method(c) for c in charges}
    return [(first, methods.pop() if len(methods) == 1 else CARD_PAYMENT, prepaid)]


def _own_rows(trip: Reservation) -> list[tuple[Charge, str, Decimal]]:
    """`(charge whose date it takes, method, amount)` for the money moved for this trip
    itself: each succeeded trip charge and captured cleaning hold, and each refund
    (negative). The rows of a refund paid back across several charges
    (`Charge.receipt_parent`) are one row at the first one's date while they went back the
    same way; a part that went to another card is its own row, so no card is named for
    money it did not get. Only rows tied to this trip are read, so a part whose head is
    not among them stands alone."""
    own = list(
        Charge.objects.filter(
            reservation=trip,
            status=Charge.Status.SUCCEEDED,
            kind__in=(*TRIP_MONEY_KINDS, Charge.Kind.REFUND),
        ).order_by("created_at", "pk")
    )
    by_pk = {c.pk: c for c in own}

    def head(charge: Charge) -> Charge:
        seen = {charge.pk}
        while charge.receipt_parent_id in by_pk and charge.receipt_parent_id not in seen:
            charge = by_pk[charge.receipt_parent_id]
            seen.add(charge.pk)
        return charge

    rows: dict[tuple[int, str], tuple[Charge, str, Decimal]] = {}
    for charge in own:
        first, method = head(charge), _method(charge)
        sign = -1 if charge.kind == Charge.Kind.REFUND else 1
        _, _, so_far = rows.get((first.pk, method), (first, method, ZERO))
        rows[(first.pk, method)] = (first, method, so_far + sign * Decimal(charge.amount))
    return list(rows.values())


def _payments(
    trip: Reservation, money: trip_money.TripMoney, total: Decimal, zone: ZoneInfo
) -> list[tuple[str, str, Decimal]]:
    """`(date, method, amount)` for the trip's own money, oldest first: its share of what
    the order prepaid (`_prepaid_rows`), then the charges and refunds made for the trip
    (`_own_rows`). Those rows add up to `money.allocated`. An on-account order then has
    one more, undated: "Billed to your account" for what the card rows leave of `total`
    (all of it when there are none, nothing when a card paid it all), so its rows add up
    to the total."""
    lead = trip.lead
    plan = getattr(lead, "payment", None)
    moved: list[tuple[Charge, str, Decimal]] = []
    if plan is not None:
        moved = [*_prepaid_rows(plan, money.prepaid), *_own_rows(trip)]
    undated = [c.pk for c, _, _ in moved if not (c.captured_at or c.receipt_due_at)]
    posted = posted_at(undated) if undated else {}
    dated = sorted(
        ((_moved_at(c, posted), method, amount) for c, method, amount in moved),
        key=lambda row: row[0],
    )
    rows = [(_day(at, zone), method, amount.quantize(_CENTS)) for at, method, amount in dated]
    if lead.on_account:
        rest = (total - sum((amount for _, _, amount in rows), ZERO)).quantize(_CENTS)
        if rest > 0:
            rows.append(("", ON_ACCOUNT, rest))
    return rows


def _company() -> dict:
    """The company block, from settings. `address`, `website` and `billing_email` are
    optional (`COMPANY_ADDRESS`, `COMPANY_WEBSITE`, `COMPANY_BILLING_EMAIL`): a blank one
    is left off the receipt, never a placeholder."""
    return {
        "name": settings.COMPANY_NAME,
        "address": settings.COMPANY_ADDRESS.strip(),
        "phone": dispatch_phone()["display"],
        "email": settings.COMPANY_EMAIL,
        "website": settings.COMPANY_WEBSITE.strip(),
        "billing_email": settings.COMPANY_BILLING_EMAIL.strip(),
    }


def receipt_context(trip: Reservation) -> dict:
    """Everything the receipt prints, as plain values. `trip` comes from
    `selectors.trip_or_404`. Customer-facing only: never vendor pay, affiliate cost, a note
    or a Stripe id."""
    lead = trip.lead
    zone = _zone(trip)
    money = trip_money.money_for(trip)
    total = customer_total(trip, money.allocated)
    if trip.trip_status in CANCELLED:
        # The money kept (a plain cancellation) or the settled price (a processed late
        # cancel), as one line.
        lines = [(CANCELLATION, total)]
    else:
        # Completed or no-show: processing settles to the price, itemised as quoted.
        lines = [(line.label, line.amount) for line in trip.price_lines()]
    payments = _payments(trip, money, total, zone)
    # What is still owed (or, negative, the customer's credit). An on-account trip owes
    # nothing here: what its card rows leave is billed to the account, as its last row.
    balance_due = None
    if payments:
        left = (total - sum((a for _, _, a in payments), ZERO)).quantize(_CENTS)
        if not lead.on_account or left < 0:
            balance_due = left
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


def _transliterations() -> dict[str, str]:
    """Letters the core fonts have no glyph for and Unicode gives no plain form of, as the
    Latin a reader would write: Latin letters with a stroke, Greek, Cyrillic (Russian and
    Ukrainian), and a few dashes and primes. An upper-case letter is its lower-case one's
    answer, capitalised ("Щ" -> "Shch")."""
    lower = {
        **dict(zip("łđħıĸŧəǝ", "ldhiktee", strict=True)),
        "ŋ": "ng",
        **dict(zip("αβγδεζηικλμνξοπρσςτυφω", "avgdeziiklmnxoprsstufo", strict=True)),
        **{"θ": "th", "χ": "ch", "ψ": "ps"},
        **dict(zip("абвгдеёзийклмнопрстуфыэіґ", "abvgdeeziyklmnoprstufyeig", strict=True)),
        **{"ж": "zh", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch"},
        **{"ю": "yu", "я": "ya", "є": "ye", "ї": "yi", "ъ": "", "ь": ""},
    }
    table = dict(lower)
    for char, plain in lower.items():
        if char.upper() != char and len(char.upper()) == 1:
            table.setdefault(char.upper(), plain.capitalize())
    table.update(dict.fromkeys("\u2010\u2011\u2012\u2212", "-"))
    table.update({"\u2015": "\u2014", "\u2032": "'", "\u2033": '"'})
    return table


_TRANSLITERATED = _transliterations()


def _has_glyph(text: str) -> bool:
    try:
        text.encode(ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _printable(char: str) -> str:
    """One character as the core fonts can draw it: itself when they have it, else its
    nearest form. In order: the same letter with fewer accents ("ễ" -> "ê"), its plain
    letters ("ř" -> "r", "ﬁ" -> "fi", "Ｆ" -> "F"), its transliteration ("Щ" -> "Shch"),
    and "?" for a script with no Latin reading here (Chinese, Arabic, an emoji)."""
    if _has_glyph(char):
        return char
    if char in _TRANSLITERATED:
        return _TRANSLITERATED[char]
    if unicodedata.category(char).startswith("M"):
        return ""  # a stray combining mark with no letter to sit on
    parts = unicodedata.normalize("NFKD", char)
    if parts == char:
        return "?"
    letters = [c for c in parts if not unicodedata.category(c).startswith("M")]
    marks = [c for c in parts if unicodedata.category(c).startswith("M")]
    if len(letters) == 1:
        for keep in range(len(marks) - 1, 0, -1):
            nearer = unicodedata.normalize("NFC", letters[0] + "".join(marks[:keep]))
            if len(nearer) == 1 and _has_glyph(nearer):
                return nearer
    return "".join(_printable(c) for c in letters)


def pdf_text(text: str) -> str:
    """`text` as the core fonts can print it, never raising, and as near to what was typed
    as Windows-1252 allows: a name on an expense document should read as the name.

    Composed first (NFC), so an accent typed as a separate mark ("e" + U+0301, as macOS
    and some phones send it) prints as its letter. A tab or line break becomes a space;
    every other control or invisible formatting character (C0, C1, zero-width, direction
    marks) is dropped. A character the fonts lack becomes its nearest printable form
    (`_printable`), "?" only where there is none."""
    out: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        category = unicodedata.category(char)
        if category == "Cc":
            # One space for a run of them ("\r\n"), and none after a space already there.
            if char in "\t\n\r\v\f" and out and out[-1] != " ":
                out.append(" ")
        elif category != "Cf":
            out.append(_printable(char))
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
        # Nothing is ever drawn below the bottom margin: a long text breaks onto the next
        # page between lines, and `room` moves a whole block (a row, the side-by-side
        # columns, a label with its first line) before fpdf would split it.
        self.set_auto_page_break(True, margin=MARGIN)
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
        """Wrapped text at `x` (the left margin by default), leaving y under it. A text
        longer than the page has room for runs onto the next one, line by line."""
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
    details = " · ".join(
        company[key] for key in ("address", "phone", "email", "website") if company[key]
    )
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
    # The columns sit side by side, so they move to a new page together. Each holds a
    # bounded amount (`NAMED_PASSENGERS`, the fields' own limits), well under a page.
    pdf.room(
        max(
            (
                pdf.height_of(title, width, 9)
                + sum(
                    pdf.height_of(text, width, 10.5 if color == INK else 9, bold=bold)
                    for text, bold, color in parts
                )
                for title, parts in columns
            ),
            default=0,
        )
    )
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
    pdf.room(9 * 1.4 + 12 * 1.4)  # the label stays with the first line under it
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
        balance = context["balance_due"]
        if balance is not None and balance < 0:
            # More was paid than the trip came to: the rest is the customer's credit.
            pdf.row("Credit", _money(-balance), bold=True, rule=False)
        elif balance is not None:
            pdf.row("Balance due", _money(balance), bold=True, rule=False)
    company = context["company"]
    thanks = f"Thank you for riding with {company['name']}."
    ask = company["billing_email"] or company["email"]
    if ask:
        thanks += f" Questions about this receipt: {ask}"
    pdf.ln(24)
    pdf.room(pdf.height_of(thanks, pdf.width, 9))
    pdf.para(thanks, pdf.width, 9, color=MUTED)
    return bytes(pdf.output())
