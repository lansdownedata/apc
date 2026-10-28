"""Trip Processing (APC-102, spec 2026-10-25 §4).

A billing agent works each finished trip on its own: confirms the times, settles the
overtime and the price, charges or refunds the customer, and locks it. This module decides
which trips are ready, groups them by customer for the list, and (Task 3) processes,
unlocks and adds a charge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta
from decimal import Decimal

from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.dispatch.selectors import coverage_prefetch

from .models import Reservation, Stop, TripReview

S = Reservation.TripStatus
# Statuses processing settles (spec §4.1): the trip ran, or it carries a charge.
SETTLE_STATUSES = (S.DONE, S.LATE_CANCEL, S.NO_SHOW)
# Never processed: nothing ran and nothing is owed.
NEVER = (S.CANCELLED, S.COVID_CANCELLATION)
# A status LA or dispatch sets when the trip is over. Anything else on a trip whose
# scheduled end has passed means the status never arrived: the trip "needs a status".
TERMINAL = (*SETTLE_STATUSES, S.CANCELLED_BY_AFFILIATE, *NEVER)
DEFAULT_DAYS = 30  # the Processed view's window


@dataclass
class Filters:
    view: str = "ready"  # "ready" | "bills" | "processed"
    q: str = ""
    date_from: date | None = None
    date_to: date | None = None

    issues: bool = False  # only trips with an open issue (the exceptions drill-down)

    @classmethod
    def from_query(cls, query) -> Filters:
        """Ready has no rolling window — it lists every unprocessed finished trip from
        `TaskConfig.processing_from` — so a trip never drops off unprocessed. Processed
        defaults to the last 30 days of pickups."""
        view = query.get("view") or "ready"
        view = view if view in ("ready", "bills", "processed") else "ready"
        date_from, date_to = _day(query.get("from")), _day(query.get("to"))
        if view == "processed" and date_from is None:
            date_from = timezone.localdate() - timedelta(days=DEFAULT_DAYS)
        return cls(
            view=view,
            q=(query.get("q") or "").strip(),
            date_from=date_from,
            date_to=date_to,
            issues=query.get("issues") == "1",
        )


def _day(raw) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@dataclass
class CustomerGroup:
    contact: object
    trips: list = field(default_factory=list)

    @property
    def oldest(self) -> date | None:
        return self.trips[0].pickup_date if self.trips else None

    @property
    def pays(self) -> str:
        """How this customer's listed trips are paid: their card, "Invoice" for orders billed
        to an account (APC-105), or both when they have one of each."""
        invoiced = any(t.lead.on_account for t in self.trips)
        card_trips = [t for t in self.trips if not t.lead.on_account]
        if not card_trips:
            return "Invoice"
        plans = [getattr(t.lead, "payment", None) for t in card_trips]
        card = next((p for p in plans if p is not None and p.stripe_payment_method_id), None)
        label = (
            f"Card ••{card.card_last4}"
            if card is not None and card.card_last4
            else ("Card on file" if card is not None else "No card")
        )
        return f"{label} · Invoice" if invoiced else label


def _base(filters: Filters):
    from apps.tasks.models import TaskConfig

    qs = (
        Reservation.objects.filter(lead__status="booked", pickup_date__isnull=False)
        .exclude(trip_status__in=NEVER)
        .select_related("lead__contact", "lead__payment", "vehicle", "review")
        .prefetch_related(
            Prefetch("stops", queryset=Stop.objects.order_by("sequence")),
            coverage_prefetch(),
        )
    )
    # Read without `load()`: the exceptions counts run this on every page, and creating
    # the settings row there would cost queries (no row yet = no floor).
    floor = TaskConfig.objects.values_list("processing_from", flat=True).first()
    if floor is not None:
        qs = qs.filter(pickup_date__gte=floor)
    if filters.date_from is not None:
        qs = qs.filter(pickup_date__gte=filters.date_from)
    if filters.date_to is not None:
        qs = qs.filter(pickup_date__lte=filters.date_to)
    if filters.issues:
        qs = qs.filter(
            review__issues__isnull=False, review__issues__resolved_at__isnull=True
        ).distinct()
    if filters.q:
        match = Q(lead__contact__name__icontains=filters.q)
        pk = _reference_pk(filters.q)
        if pk is not None:
            match |= Q(pk=pk)
        qs = qs.filter(match)
    return qs


def _reference_pk(q: str) -> int | None:
    """ "APC-600142" (any case) → 142, the trip's pk (`Reservation.reference`)."""
    from apps.leads.models import QUOTE_PREFIX

    text = q.strip().upper()
    prefix = f"{QUOTE_PREFIX}-"
    if text.startswith(prefix) and text[len(prefix) :].isdigit():
        n = int(text[len(prefix) :]) - 600_000
        return n if n > 0 else None
    return None


def _short(stop) -> str:
    """A stop as the list shows it: its name, else the first part of its address."""
    if stop is None:
        return ""
    return stop.name or (stop.address or "").split(",")[0]


def _with_route(trips) -> list:
    """`trip.route_short` ("The Jefferson → Congressional CC") from the prefetched stops —
    never `trip.pickup` / `.last()`, which query per row."""
    for trip in trips:
        stops = list(trip.stops.all())
        trip.route_short = (
            f"{_short(stops[0])} → {_short(stops[-1])}"
            if len(stops) > 1
            else _short(stops[0])
            if stops
            else ""
        )
    return trips


def _order(trips) -> list:
    return sorted(trips, key=lambda t: (t.pickup_date or date.max, t.pickup_time or time.max, t.pk))


def ready_trips(filters: Filters) -> list[Reservation]:
    """Finished trips not yet processed (spec §4.1): Done, Late Cancel, No Show; Cancelled
    by Affiliate only when there is money to settle; and a trip with no status whose
    scheduled end + grace has passed ("needs a status" — LA statuses can be missing)."""
    from apps.payments import trip_money
    from apps.tasks import post_trip
    from apps.tasks.models import TaskConfig

    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    now = timezone.now()
    out = []
    unprocessed = _base(filters).filter(
        Q(review__isnull=True) | Q(review__processed_at__isnull=True)
    )
    for trip in unprocessed:
        trip.needs_status = trip.trip_status not in TERMINAL
        if trip.trip_status in SETTLE_STATUSES:
            out.append(trip)
        elif trip.trip_status == S.CANCELLED_BY_AFFILIATE:
            money = trip_money.money_for(trip)
            if money.allocated != 0 or money.line_total != 0:
                out.append(trip)
        elif trip.needs_status and post_trip.has_ended(trip, now=now, grace=grace):
            out.append(trip)
    return _with_route(_order(out))


def workable(trip) -> bool:
    """Whether the processing pane — and every endpoint it posts to — may work this trip:
    a booked order, not a plain cancellation, and either settled by status or over by the
    clock (spec §4.1). No-shows and late cancels are workable; they carry the charge."""
    from apps.tasks import post_trip
    from apps.tasks.models import TaskConfig

    if trip.lead.status != "booked" or trip.trip_status in NEVER:
        return False
    if trip.trip_status in (*SETTLE_STATUSES, S.CANCELLED_BY_AFFILIATE):
        return True
    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    return post_trip.has_ended(trip, now=timezone.now(), grace=grace)


def lock_message(trip) -> str:
    """Why a trip can't be edited, or "" when it can (spec §3.4, §4.6)."""
    review = TripReview.objects.filter(reservation_id=trip.pk, processed_at__isnull=False).first()
    if review is None:
        return ""
    who = review.processed_by
    name = (who.get_full_name() or who.username) if who else "someone"
    from zoneinfo import ZoneInfo

    from django.conf import settings

    zone = ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)
    when = review.processed_at.astimezone(zone).strftime("%b %-d")
    return f"Processed on {when} by {name}. Unlock it from Trip Processing to change the price."


def any_processed(trips) -> bool:
    return TripReview.objects.filter(
        reservation__in=list(trips), processed_at__isnull=False
    ).exists()


def needs_status(trip) -> bool:
    return trip.trip_status not in TERMINAL


# How each view can be sorted (Moe 2026-10-25): oldest first by default on Ready; each
# user's choice is remembered (`User.preferences["processing"]`).
READY_SORTS = {
    "oldest": "Oldest trip first",
    "newest": "Newest trip first",
    "customer": "Customer A to Z",
    "most": "Most trips first",
}
PROCESSED_SORTS = {
    "processed": "Recently processed",
    "oldest": "Oldest pickup first",
    "newest": "Newest pickup first",
    "customer": "Customer A to Z",
}
SORTS = {"ready": READY_SORTS, "processed": PROCESSED_SORTS}


def processed_trips(filters: Filters, sort: str = "processed") -> list[Reservation]:
    """Processed trips in the range, the most recently processed first unless `sort` says
    otherwise."""
    trips = list(_base(filters).filter(review__processed_at__isnull=False))
    for trip in trips:
        trip.needs_status = False
    if sort == "oldest":
        trips = _order(trips)
    elif sort == "newest":
        trips = _order(trips)[::-1]
    elif sort == "customer":
        trips = sorted(_order(trips), key=lambda t: t.lead.contact.name.casefold())
    else:
        trips = sorted(trips, key=lambda t: t.review.processed_at, reverse=True)
    return _with_route(trips)


def issue_trip_count() -> int:
    """How many trips the issues drill-down lists, in one query (the exceptions counts run
    on every page): booked orders, from the processing start, not a plain cancellation,
    with an open issue."""
    return _base(Filters(issues=True)).values("pk").distinct().count()


def issue_trips() -> list[Reservation]:
    """Every trip with an open issue, processed or not (the exceptions drill-down) — the
    same rows `issue_trip_count` counts."""
    trips = list(_base(Filters(issues=True)))
    for trip in trips:
        trip.needs_status = trip.trip_status not in TERMINAL
    return _with_route(_order(trips))


def group_by_customer(trips, sort: str = "oldest") -> list[CustomerGroup]:
    """One list, grouped by the order's contact (Moe rejected two side-by-side lists).
    Customers by their oldest trip first unless `sort` says otherwise (newest trip, name,
    most trips); trips in pickup order within each, newest first under "newest"."""
    groups: dict[int, CustomerGroup] = {}
    ordered = _order(trips)
    if sort == "newest":
        ordered = ordered[::-1]
    for trip in ordered:
        contact = trip.lead.contact
        groups.setdefault(contact.pk, CustomerGroup(contact=contact)).trips.append(trip)
    out = list(groups.values())
    oldest = lambda g: (min(t.pickup_date for t in g.trips), g.contact.pk)  # noqa: E731
    if sort == "newest":
        return sorted(
            out, key=lambda g: (max(t.pickup_date for t in g.trips), g.contact.pk), reverse=True
        )
    if sort == "customer":
        return sorted(out, key=lambda g: (g.contact.name.casefold(), g.contact.pk))
    if sort == "most":
        return sorted(out, key=lambda g: (-len(g.trips), oldest(g)))
    return sorted(out, key=oldest)


def review_of(trip) -> TripReview | None:
    try:
        return trip.review
    except TripReview.DoesNotExist:
        return None


# --- processing (Task 3): money first, each step committing on its own; then the lock ----


class ProcessingError(ValueError):
    """Something processing can't do; the message is shown to the agent."""


COLLECT_CHOICES = ("card", "link", "refund", "keep", "invoice")
ON_ACCOUNT = "This order is billed to an account: process it for the invoice."


def invoice_note(lead) -> str:
    """The PROCESSED note and the pane's footer line for a trip billed to an account."""
    return f"Billed to {lead.billed_to_label}. Goes on the next invoice."


def _fresh(trip) -> Reservation:
    return Reservation.objects.select_related("lead__contact", "lead__payment").get(pk=trip.pk)


def _event(review, kind, *, user=None, note="", amount=None, charge=None):
    from .models import ProcessingEvent

    return ProcessingEvent.objects.create(
        review=review, kind=kind, actor=user, note=note[:255], amount=amount, charge=charge
    )


def _cancel_open_links(trip) -> None:
    """Cancel any pay link still open for this trip, so an old tab can't collect money the
    trip no longer owes. Raises when it turns out the customer just paid it."""
    from apps.payments import trip_money

    plan = getattr(trip.lead, "payment", None)
    if plan is None:
        return
    for link in plan.charges.filter(kind="trip", reservation=trip, status="pending").exclude(
        stripe_client_secret=""
    ):
        if trip_money._cancel_intent(plan, link):
            raise ProcessingError("The customer just paid by the link; check the trip again.")


def _open_link(trip):
    plan = getattr(trip.lead, "payment", None)
    if plan is None:
        return None
    return (
        plan.charges.filter(kind="trip", reservation=trip, status="pending")
        .exclude(stripe_client_secret="")
        .first()
    )


def parse_amount(raw) -> Decimal:
    """A positive money amount from a form, or ProcessingError — never a 500 (NaN, inf)."""
    from decimal import InvalidOperation

    try:
        amount = Decimal(str(raw).replace("$", "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise ProcessingError("Enter a valid amount.") from exc
    if not amount.is_finite():
        raise ProcessingError("Enter a valid amount.")
    amount = amount.quantize(Decimal("0.01"))
    if amount <= 0:
        raise ProcessingError("The amount has to be above zero.")
    return amount


def process(
    trip,
    *,
    user,
    expected,
    collect: str = "",
    reason: str = "",
    method: str = "card",
    reference: str = "",
    base_url: str = "",
    sender_name: str = "",
) -> TripReview:
    """Settle the trip's money and lock it (spec §4.4, §4.6).

    1. Checks, on a fresh row: workable; a settled status (else the agent sets one);
       not processed; the overtime decided when there is a suggestion (the agent must see
       the money they confirm); `expected` — the difference the pane showed — still true.
    2. Money, each step committing on its own (never inside the lock's transaction, so a
       later failure can't erase a charge Stripe already took): charge the card, open and
       send a pay link, refund with a reason, or keep an off-card overpayment as credit.
       A difference of 0 cancels any open pay link for the trip.
    3. The lock, in one transaction with both rows locked: refuse if processed meanwhile
       or the price moved under us; stamp completion, re-freeze driver pay, the lock and
       the history.
    4. After commit: revenue follows the trip; the trip's review tasks close.
    """
    from decimal import Decimal

    import stripe
    from django.db import transaction

    from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
    from apps.payments import services as payments
    from apps.payments import trip_money

    from . import reviews
    from .models import ProcessingEvent

    trip = _fresh(trip)
    if not workable(trip):
        raise ProcessingError("This trip can't be processed.")
    if trip.trip_status not in (*SETTLE_STATUSES, S.CANCELLED_BY_AFFILIATE):
        raise ProcessingError("Set the trip's status first.")
    review = reviews.review_for(trip)
    if review.is_processed:
        raise ProcessingError("This trip is already processed.")
    if review.decided_at is None and review.suggested_overtime_minutes > 0:
        raise ProcessingError("Decide the overtime first: keep or change the suggestion.")
    money = trip_money.money_for(trip)
    if Decimal(str(expected)).quantize(Decimal("0.01")) != money.difference:
        raise ProcessingError("The price changed; check the trip again.")

    def record(kind, amount, charge=None, note=""):
        # Written as each money step commits, so History matches the money even if the lock
        # below refuses (the agent re-processes; only the new difference is settled).
        _event(review, kind, user=user, amount=amount, charge=charge, note=note)

    on_account = trip.lead.on_account
    if money.difference > 0 and on_account and collect != "invoice":
        raise ProcessingError(ON_ACCOUNT)
    if collect == "invoice" and not on_account:
        raise ProcessingError("Charge the card or send a payment link.")

    try:
        if money.difference > 0 and collect == "invoice":
            # Never a card action (spec §4.4): the price stays owed, for the invoice.
            _cancel_open_links(trip)
        elif money.difference > 0:
            if collect == "card":
                charge = trip_money.charge_trip(trip, money.difference)
                record(ProcessingEvent.Kind.CHARGED, money.difference, charge)
            elif collect == "link":
                from apps.core import outbound

                if not outbound.enabled():
                    raise ProcessingError(outbound.OFF_MESSAGE)
                trip_money.open_trip_intent(trip, money.difference)
                payments.send_pay_link(trip.lead, base_url=base_url, sender_name=sender_name)
                record(ProcessingEvent.Kind.PAY_LINK_SENT, money.difference)
            else:
                raise ProcessingError("Charge the card or send a payment link.")
        elif money.difference < 0:
            owed_back = -money.difference
            _cancel_open_links(trip)
            if collect == "refund":
                refunded = trip_money.refund_trip(
                    trip, owed_back, reason=reason, user=user, method=method, reference=reference
                )
                record(ProcessingEvent.Kind.REFUNDED, refunded, note=reason.strip())
            elif collect == "keep":
                # Money paid off-card has no Stripe intent to refund; the overpayment stays
                # with the order and the PROCESSED event below says so.
                if not (reason or "").strip():
                    raise ProcessingError("Say why the overpayment is kept.")
            else:
                raise ProcessingError("Refund the overpayment, or hold it as credit.")
        else:
            _cancel_open_links(trip)
    except payments.PaymentError as exc:
        raise ProcessingError(str(exc)) from exc
    except stripe.error.StripeError as exc:
        raise ProcessingError(getattr(exc, "user_message", None) or "Stripe refused it.") from exc
    except (PodiumAPIError, PodiumNotConnected) as exc:
        raise ProcessingError(f"The link didn't send: {exc}") from exc

    with transaction.atomic():
        locked = Reservation.objects.select_for_update().get(pk=trip.pk)
        review = TripReview.objects.select_for_update().get(pk=review.pk)
        if review.is_processed:
            raise ProcessingError("Someone else processed this trip just now.")
        from apps.leads.models import Lead

        # How the order is paid can't move under us: `bill_to_account` / `bill_to_card` lock
        # the order too, and refuse once a trip is processed.
        order = Lead.objects.select_for_update().get(pk=locked.lead_id)
        if order.on_account != on_account:
            raise ProcessingError("How this order is paid just changed; check the trip again.")
        settled_to = trip_money.money_for(locked).difference
        expected_after = (
            money.difference if collect in ("link", "keep", "invoice") else Decimal("0.00")
        )
        if settled_to != expected_after:
            raise ProcessingError(
                "The price changed while processing; check the trip and process it again."
            )
        now = timezone.now()
        if review.completed_at is None:
            review.completed_at, review.completed_by = now, user
        reviews._stamp_driver_pay(review)
        # The share of the order's prepaid money this trip was settled against; kept while
        # it stays processed (see trip_money.allocation).
        review.prepaid_share = trip_money.allocation(locked.lead).get(locked.pk, Decimal("0.00"))
        review.processed_at, review.processed_by = now, user
        review.save()
        note = ""
        if collect == "keep":
            note = (
                f"Overpayment of ${-money.difference:,.2f} held as credit owed to the"
                f" customer: {reason.strip()}"
            )
        elif collect == "refund":
            note = (reason or "").strip()
        elif collect == "invoice":
            note = invoice_note(locked.lead)
        _event(review, ProcessingEvent.Kind.PROCESSED, user=user, note=note)

    _after_lock(locked, user, keep_overtime_open=collect == "link")
    return review


def _after_lock(trip, user, *, keep_overtime_open: bool) -> None:
    """Outside the lock: the books follow the trip, the payable exists, the review tasks
    close. Each step is idempotent; a failure here never undoes the lock."""
    import logging

    from apps.payments import ledger, payables
    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    log = logging.getLogger(__name__)
    trip.refresh_from_db()
    try:
        ledger.true_up_recognition(trip)
    except Exception:  # noqa: BLE001 - the nightly job and the next process heal it
        log.exception("true-up after processing trip %s failed", trip.pk)
    try:
        payables.ensure_payable(trip)
    except Exception:  # noqa: BLE001
        log.exception("payable for processed trip %s failed", trip.pk)
    # In order: closing ops_review opens overtime_invoiced (opens_after), which processing
    # has just settled too — so it is looked up again after, not loaded up front.
    kinds = ["ops_review"] if keep_overtime_open else ["ops_review", "overtime_invoiced"]
    for kind in kinds:
        for task in Task.objects.filter(reservation=trip, kind=kind, status__in=Task.UNRESOLVED):
            try:
                tasks.complete(task, user)
            except tasks.TaskError:
                continue


def unlock(trip, *, user, reason: str) -> TripReview:
    """Reopen a processed trip (spec §4.6): payments access is the view's gate; a reason is
    required and recorded. The trip's review task reopens; re-processing settles only the
    new difference."""
    from django.db import transaction

    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    from .models import ProcessingEvent

    reason = (reason or "").strip()
    if not reason:
        raise ProcessingError("Say why the trip is unlocked.")
    trip = _fresh(trip)
    if trip.lead.status != "booked":
        raise ProcessingError("Only a booked order's trips can be unlocked.")
    # The price is about to change: an old pay link must not stay collectable.
    _cancel_open_links(trip)
    with transaction.atomic():
        review = TripReview.objects.select_for_update().filter(reservation=trip).first()
        if review is None or not review.is_processed:
            raise ProcessingError("This trip isn't processed.")
        review.processed_at = review.processed_by = None
        review.unlocked_at, review.unlocked_by = timezone.now(), user
        review.unlock_reason = reason[:255]
        review.prepaid_share = None  # back into the order's allocation until re-processed
        review.save()
        _event(review, ProcessingEvent.Kind.UNLOCKED, user=user, note=reason)
    for task in Task.objects.filter(reservation=trip, kind="ops_review", status__in=Task.CLOSED):
        tasks.reopen(task, user)
    return review


def add_charge(
    trip, *, user, amount, label: str, collect: str = "card", base_url="", sender_name=""
):
    """Processed-view convenience (spec §4.7): a new line on the trip, then exactly that
    amount charged to the card or sent as a link. The line commits first; if the money
    step fails it is deleted again, so the price and the money never disagree."""
    import stripe

    from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
    from apps.payments import services as payments
    from apps.payments import trip_money

    from .models import ProcessingEvent, TripExtra

    trip = _fresh(trip)
    if trip.lead.status != "booked":
        raise ProcessingError("Only a booked order's trips can be charged.")
    review = review_of(trip)
    if review is None or not review.is_processed:
        raise ProcessingError("Add a charge from a processed trip; otherwise edit the price.")
    label = (label or "").strip()[:80]
    if not label:
        raise ProcessingError("Say what the charge is for.")
    if trip.lead.on_account and collect != "invoice":
        raise ProcessingError(ON_ACCOUNT)
    if collect == "invoice" and not trip.lead.on_account:
        raise ProcessingError("Charge the card or send a payment link.")
    amount = parse_amount(amount)
    if _open_link(trip) is not None:
        raise ProcessingError(
            "A payment link is still open for this trip. Wait for it, or unlock and re-process."
        )
    extra = TripExtra.objects.create(
        reservation=trip, kind=TripExtra.Kind.OTHER, label=label, amount=amount, created_by=user
    )
    # Credit the customer already has on the trip (a kept overpayment) covers it first.
    to_collect = min(amount, trip_money.money_for(_fresh(trip)).difference)
    if to_collect <= 0 or collect == "invoice":
        covered = to_collect <= 0
        note = f"{label} (covered by the trip's credit)" if covered else f"{label}: on the invoice"
        _event(review, ProcessingEvent.Kind.CHARGE_ADDED, user=user, amount=amount, note=note)
        trip.refresh_from_db()
        from apps.payments import ledger

        ledger.true_up_recognition(trip)
        return None
    # The receipt says what the charge is, and what credit covered (APC-103).
    lines = [[label, amount]]
    if to_collect < amount:
        lines.append(["Credit applied", -(amount - to_collect)])
    amount = to_collect
    try:
        if collect == "link":
            from apps.core import outbound

            if not outbound.enabled():
                raise ProcessingError(outbound.OFF_MESSAGE)
            trip_money.open_trip_intent(trip, amount, note=label, lines=lines)
            payments.send_pay_link(trip.lead, base_url=base_url, sender_name=sender_name)
            charge = None
            kind = ProcessingEvent.Kind.PAY_LINK_SENT
        else:
            charge = trip_money.charge_trip(trip, amount, note=label, lines=lines)
            kind = ProcessingEvent.Kind.CHARGE_ADDED
    except (
        ProcessingError,
        payments.PaymentError,
        stripe.error.StripeError,
        PodiumAPIError,
        PodiumNotConnected,
    ) as exc:
        extra.delete()
        message = getattr(exc, "user_message", None) or str(exc) or "That didn't go through."
        raise ProcessingError(message) from exc
    _event(review, kind, user=user, note=label, amount=amount, charge=charge)
    trip.refresh_from_db()
    from apps.payments import ledger

    ledger.true_up_recognition(trip)
    return charge


__all__ = [
    "SETTLE_STATUSES",
    "CustomerGroup",
    "Filters",
    "group_by_customer",
    "processed_trips",
    "ready_trips",
    "review_of",
    "needs_status",
    "workable",
    "ProcessingError",
    "process",
    "unlock",
    "add_charge",
]


# --- Needs vendor bill (APC-104, spec §6) -------------------------------------------------

# Vendor bills: oldest waiting first by default, like Ready.
BILL_SORTS = {
    "oldest": "Oldest waiting first",
    "vendor": "Vendor A to Z",
    "most": "Most trips first",
}
SORTS["bills"] = BILL_SORTS
# Statuses with nobody to pay: `ensure_payable` makes no payable for them, and one made
# earlier (the pane opened before LA said the affiliate dropped it) is not a bill.
NO_VENDOR_BILL = (*NEVER, S.CANCELLED_BY_AFFILIATE)


@dataclass
class VendorGroup:
    vendor: object
    trips: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.trips)

    @property
    def expected_total(self) -> Decimal:
        return sum((Decimal(t.payable.expected_amount) for t in self.trips), Decimal("0.00"))

    @property
    def oldest(self) -> date | None:
        return self.trips[0].pickup_date if self.trips else None


def _bill_queryset(*, vendor=None, q: str = ""):
    """Processed trips on a booked order whose confirmed vendor has no invoice on file: the
    payable is missing (processing's `ensure_payable` failed, or predates it) or a draft
    with no amount. One `filter()` call, so every condition reads the same assignment —
    a withdrawn first vendor never stands in for the confirmed one."""
    from apps.dispatch.models import Assignment
    from apps.payments.models import AffiliatePayable

    match = Q(
        assignments__status=Assignment.Status.CONFIRMED, assignments__vendor__isnull=False
    ) & (
        Q(assignments__payable__isnull=True)
        | Q(
            assignments__payable__status=AffiliatePayable.Status.DRAFT,
            assignments__payable__invoice_amount__isnull=True,
        )
    )
    if vendor is not None:
        match &= Q(assignments__vendor=vendor)
    if q:
        found = Q(lead__contact__name__icontains=q) | Q(assignments__vendor__name__icontains=q)
        pk = _reference_pk(q)
        if pk is not None:
            found |= Q(pk=pk)
        match &= found
    return (
        Reservation.objects.filter(
            match,
            lead__status="booked",
            review__processed_at__isnull=False,
            pickup_date__isnull=False,
        )
        .exclude(trip_status__in=NO_VENDOR_BILL)
        .distinct()
    )


def vendor_bills(filters: Filters, sort: str = "oldest") -> list[VendorGroup]:
    """The Needs vendor bill list: one group per vendor, its trips oldest pickup first, each
    carrying `.payable`, `.vendor` and `.route_short`. A processed trip whose payable was
    never made gets it here, as the trip pane does when it opens — a few queries for each
    such trip, once (rare: processing makes it). If making it fails, that trip is left out
    and logged rather than taking the view down for everyone."""
    import logging

    from apps.dispatch.models import Assignment
    from apps.payments import payables
    from apps.payments.models import AffiliatePayable

    qs = _bill_queryset(q=filters.q)
    if filters.date_from is not None:
        qs = qs.filter(pickup_date__gte=filters.date_from)
    if filters.date_to is not None:
        qs = qs.filter(pickup_date__lte=filters.date_to)
    trips = qs.select_related("lead__contact").prefetch_related(
        Prefetch("stops", queryset=Stop.objects.order_by("sequence")),
        Prefetch(
            "assignments",
            queryset=Assignment.objects.filter(
                status=Assignment.Status.CONFIRMED, vendor__isnull=False
            ).select_related("vendor", "payable"),
            to_attr="vendor_assignments",
        ),
    )
    groups: dict[int, VendorGroup] = {}
    for trip in _with_route(_order(trips)):
        assignment = trip.vendor_assignments[0]
        try:
            trip.payable = assignment.payable
        except AffiliatePayable.DoesNotExist:
            try:
                trip.payable = payables.ensure_payable(trip)
            except Exception:  # noqa: BLE001 - one bad trip mustn't break the list
                logging.getLogger(__name__).exception("payable for trip %s failed", trip.pk)
                continue
            if trip.payable is None:
                continue
        trip.vendor = assignment.vendor
        groups.setdefault(trip.vendor.pk, VendorGroup(trip.vendor)).trips.append(trip)
    out = list(groups.values())
    if sort == "vendor":
        return sorted(out, key=lambda g: g.vendor.name.casefold())
    if sort == "most":
        return sorted(out, key=lambda g: (-g.count, g.oldest, g.vendor.name.casefold()))
    return sorted(out, key=lambda g: (g.oldest, g.vendor.name.casefold()))


def vendor_bill_count() -> int:
    """The tab's count: every trip waiting for a vendor bill (one query, no filters)."""
    return _bill_queryset().count()


def vendor_bill_trip_ids(vendor) -> set[int]:
    """The vendor's trips the Needs vendor bill view lists — the vendor page links those
    there, and the rest of its open payables to the trip's own pane."""
    return set(_bill_queryset(vendor=vendor).values_list("pk", flat=True))
