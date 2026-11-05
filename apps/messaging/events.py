"""Queueing messages (spec C sections 3, 5 and 9): what creates a TouchPoint, and when.

Every message row names a trigger (`triggers.CATALOG`). This module turns a trigger into
TouchPoints, one per row on it that is on and whose service types match
(`rules.matches`), each carrying its row (`message=row`, `kind=row.key`):

- `emit(trigger, lead, reservation=None)`: an app event happened now.
- `schedule_dates(lead, reservation=None)`: make sure every date row (pickup, quote expiry,
  balance due) has its TouchPoint, timed from its anchor.
- `reschedule_trip(reservation)`: a trip's pickup moved; its pickup rows start over.
- `sync_waiting(reservation)`: rows that wait for a condition (Dispatched with driver
  details) are queued or re-dated once it holds.
- `cancel(lead, ...)`, `withdraw(row)` and `redate(row, old_offset=...)`: queued sends
  are withdrawn (for an order, or for a row everywhere) or re-timed.

Whether a row "already has" its TouchPoint follows the old scheduler (the
`touchpoints.schedule_*` functions this replaced) rule by rule: one per order for
order-level rows, one per trip for trip-level rows, and a cancelled TouchPoint never
counts. The exceptions are in `_REPEATS`. Anything due the moment it is queued goes out
once the transaction commits (`sender.send_due`), not on the next cron run: that is the
catch-up of spec section 5.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import rules, sender
from . import triggers as catalog
from .models import Delivery, MessageTemplate, NotificationConfig, TouchPoint

_S = TouchPoint.Status

# Every trigger, for a cancel that sweeps the whole order (a lost lead).
EVERY = frozenset(catalog.CATALOG)
# What `cancel` withdraws by default: the pre-booking program (welcome, quote and expiry
# nudges) and the order outcome messages, as the old `cancel_pending` did. The
# post-trip messages, the payment reminder and every trip-level message are spared, and so
# is every event the old scheduler never queued (deposit paid, balance paid, wedding details
# received): booking must not withdraw the message about the payment that booked it.
PRE_BOOKING = frozenset(
    {
        "lead_created",
        "quote_sent",
        "quote_viewed",
        "quote_expiry",
        "deposit_hold_released",
        "order_confirmed",
        "order_cancelled",
    }
)
# A re-sent quote starts its program over: queued sends of these are withdrawn first.
_QUOTE_PROGRAM = ("quote_sent", "quote_viewed", "quote_expiry")
# The trip date triggers queued from the pickup when the order books (`pickup`); one that
# waits for a condition is queued by `sync_waiting` instead.
_PICKUP_TRIGGERS = tuple(
    t.key
    for t in catalog.CATALOG.values()
    if t.kind == "date" and t.level == "trip" and not t.waits_for
)

# How an earlier TouchPoint of the same row (same order, or same trip) blocks a new one.
ONCE = "once"  # anything but a cancelled one: sent, queued, skipped or failed
EVER = "ever"  # anything at all: a held (cancelled) review request stays held
# The error on a send withdrawn because its row was switched off (`withdraw`). The balance
# due notice's once-ever rule lets it through, so switching the row back on brings the
# notice back; every other cancellation still blocks.
SWITCHED_OFF = "Switched off"
PENDING = "pending"  # only a queued one: a second lapse is told a second time
FRESH = "fresh"  # nothing: the trigger withdraws its queued sends and starts over

_REPEATS: dict[str, str] = {
    "quote_sent": FRESH,
    "deposit_hold_released": PENDING,
    "order_confirmed": PENDING,
    "order_cancelled": PENDING,
    "driver_changed": PENDING,
    # Things that can happen to a trip again: a re-cancelled trip, a second affiliate.
    "trip_cancelled": PENDING,
    "affiliate_accepted": PENDING,
    "all_trips_done": EVER,
}


def _master_on() -> bool:
    return NotificationConfig.load().enabled


def _rows_on(
    trigger_keys: Iterable[str],
    *,
    enabled_only: bool = True,
    only: MessageTemplate | None = None,
) -> list[MessageTemplate]:
    """The live rows on these triggers (enabled ones unless told otherwise). ``only`` limits
    the answer to that one row, read by the caller (service types prefetched): no query."""
    keys = list(trigger_keys)
    if only is not None:
        on = only.trigger in keys and only.archived_at is None
        return [only] if on and (only.enabled or not enabled_only) else []
    qs = MessageTemplate.objects.live().filter(trigger__in=keys)
    if enabled_only:
        qs = qs.filter(enabled=True)
    return list(qs.prefetch_related("service_types").order_by("pk"))


def _of_row(row: MessageTemplate) -> Q:
    """TouchPoints of a row: those pointing at it, and the old scheduler's (no row yet,
    the row's key as their kind)."""
    return Q(message=row) | Q(message__isnull=True, kind=row.key)


def _blocked(row: MessageTemplate, lead, reservation, rule: str) -> bool:
    if rule == FRESH:
        return False
    qs = TouchPoint.objects.filter(_of_row(row), lead=lead, reservation=reservation)
    if rule == PENDING:
        qs = qs.filter(status=_S.SCHEDULED)
    elif rule == ONCE:
        qs = qs.exclude(status=_S.CANCELLED)
    return qs.exists()


def _create(row: MessageTemplate, lead, reservation, at: datetime) -> TouchPoint:
    return TouchPoint.objects.create(
        lead=lead, reservation=reservation, message=row, kind=row.key, scheduled_for=at
    )


def _send_on_commit(tps: Iterable[TouchPoint]) -> None:
    """Send what is already due once the transaction commits (the cron sends the rest)."""
    now = timezone.now()
    ids = [tp.pk for tp in tps if tp.scheduled_for is not None and tp.scheduled_for <= now]
    if ids:
        # Robust: whatever the send does, the caller's save has committed and stands.
        transaction.on_commit(lambda: sender.send_due(ids), robust=True)


# --- events ----------------------------------------------------------------------------


# Seeded rows that are never queued while they stay on their own trigger. The Dispatched
# status text is retired: the driver details message covers dispatch (today it is never
# scheduled). A message an Admin adds on that trigger is queued as usual.
_RETIRED = frozenset({"status_dispatched"})


def _retired(row: MessageTemplate) -> bool:
    return row.key in _RETIRED and rules.has_built_ins(row)


def _thanked(lead) -> bool:
    """A plain thank-you already sent on this order, or still queued from before this event
    (the old engine's, which the sender sends in the wedding words on a wedding order).
    Read before the event queues its own rows."""
    return TouchPoint.objects.filter(
        lead=lead, kind="thank_you", status__in=(_S.SENT, _S.SCHEDULED)
    ).exists()


def _is_wedding_thank_you(row: MessageTemplate) -> bool:
    """The seeded wedding thank-you is the plain one in other words: once the customer has
    been thanked, or a thank-you is on its way, it is not queued as well."""
    return row.key == "thank_you_wedding" and rules.has_built_ins(row)


def _moment(trigger_key: str, lead, now: datetime) -> datetime:
    """When the event happened. A quote's follow-ups count from when it was sent, which a
    reissued quote keeps (as the old `schedule_quote_sent` did)."""
    if trigger_key == "quote_sent" and lead.quote_sent_at is not None:
        return lead.quote_sent_at
    return now


def emit(trigger_key: str, lead, reservation=None) -> list[TouchPoint]:
    """An event happened: queue every live, enabled row on it whose service types match,
    at the moment plus the row's timing. Returns the TouchPoints created.

    A trip-level trigger needs the trip; an order-level one ignores it. `quote_sent`
    withdraws the quote program still queued (sent, viewed and expiry nudges), queues its
    rows afresh, and re-makes the quote's expiry rows. Rows due now go out on commit.
    """
    trigger = catalog.get(trigger_key)
    if trigger.level == "trip" and reservation is None:
        raise ValueError(f"{trigger_key} is a trip event: pass the reservation")
    if trigger_key == "quote_sent":
        # Withdrawn even with the master switch off, as the old `schedule_quote_sent` did.
        cancel(lead, triggers=_QUOTE_PROGRAM)
    if not _master_on():
        return []
    trip = reservation if trigger.level == "trip" else None
    rule = _REPEATS.get(trigger_key, ONCE)
    at = _moment(trigger_key, lead, timezone.now())
    rows = [row for row in _rows_on([trigger_key]) if not _retired(row)]
    thanked = any(_is_wedding_thank_you(r) for r in rows) and _thanked(lead)
    created = []
    for row in rows:
        if not rules.matches(row, lead, trip):
            continue
        if _blocked(row, lead, trip, rule) or (thanked and _is_wedding_thank_you(row)):
            continue
        created.append(_create(row, lead, trip, at + row.offset))
    if trigger_key == "quote_sent":
        created += _quote_expiry(lead)
    _send_on_commit(created)
    return created


# --- dates -----------------------------------------------------------------------------


def _quote_expiry(lead, *, only: MessageTemplate | None = None) -> list[TouchPoint]:
    """The quote's expiring/expired nudges, while the quote is still open (new or quoted).

    A `before` row whose send time has passed is not made (as TP7 today). A queued row, or
    one already made for this expiry, blocks; one made for an earlier expiry (a re-sent
    quote) does not."""
    expires = lead.quote_expires_at
    if expires is None or lead.status not in (lead.Status.NEW, lead.Status.QUOTED):
        return []
    now = timezone.now()
    created = []
    for row in _rows_on(["quote_expiry"], only=only):
        at = expires + row.offset
        if row.timing_direction == row.Direction.BEFORE and at <= now:
            continue
        if not rules.matches(row, lead):
            continue
        mine = TouchPoint.objects.filter(_of_row(row), lead=lead, reservation=None)
        if (
            mine.filter(Q(status=_S.SCHEDULED) | Q(scheduled_for=at))
            .exclude(status=_S.CANCELLED)
            .exists()
        ):
            continue
        created.append(_create(row, lead, None, at))
    return created


def _ever_queued(row: MessageTemplate, lead) -> bool:
    """The once-ever rule (`EVER`), except a send withdrawn by switching the row off."""
    return (
        TouchPoint.objects.filter(_of_row(row), lead=lead, reservation=None)
        .exclude(status=_S.CANCELLED, error=SWITCHED_OFF)
        .exists()
    )


def _balance_due(lead, *, only: MessageTemplate | None = None) -> list[TouchPoint]:
    """The pre-charge notice: only for an order the balance cron will charge, so not one
    billed to an account, and only once its balance is scheduled on a card with a due date
    (as the old `schedule_payment_reminder`, which ran when the balance became
    scheduled). One per order, ever, unless the earlier one was withdrawn by switching the
    row off (`_ever_queued`)."""
    from apps.payments.models import PaymentPlan

    if lead.on_account:
        return []
    plan = getattr(lead, "payment", None)
    if (
        plan is None
        or plan.balance_status != PaymentPlan.BalanceStatus.SCHEDULED
        or not plan.stripe_payment_method_id
        or plan.balance_due_date is None
    ):
        return []
    anchor = catalog.get("balance_due").anchor(lead, None)
    created = []
    for row in _rows_on(["balance_due"], only=only):
        if rules.matches(row, lead) and not _ever_queued(row, lead):
            created.append(_create(row, lead, None, anchor + row.offset))
    return created


def _manual_farm_out(reservation) -> bool:
    """Confirmed coverage by an affiliate who acknowledges by link: not in-house, not GNet
    (whose acceptance is its acknowledgement, as the old `schedule_affiliate_confirmation`)."""
    from apps.dispatch.models import Assignment

    assignment = rules.confirmed_assignment(reservation)
    return (
        assignment is not None
        and not assignment.is_in_house
        and assignment.channel != Assignment.Channel.GNET
    )


def _pickup_rows(lead, trips: Iterable, *, only: MessageTemplate | None = None) -> list[TouchPoint]:
    """Each trip's rows on the pickup date trigger, timed from its pickup; a trip with no
    pickup date gets none. A past send time is queued anyway: a late booking still gets its
    confirmation, on commit. Affiliate rows only for a confirmed manual farm-out."""
    rows = _rows_on(_PICKUP_TRIGGERS, only=only)
    trips = [t for t in trips if t.pickup_at is not None]
    if not rows or not trips:
        return []
    # What each trip already has, in one read: a row's own TouchPoints, and the old
    # scheduler's by kind. Anything but a cancelled one counts.
    have = set(
        TouchPoint.objects.filter(lead=lead, reservation__in=[t.pk for t in trips])
        .exclude(status=_S.CANCELLED)
        .values_list("reservation_id", "message_id", "kind")
    )
    mine = {(res, message) for res, message, _ in have if message is not None}
    legacy = {(res, kind) for res, message, kind in have if message is None}
    created = []
    for trip in trips:
        pickup = trip.pickup_at
        farm_out = None
        for row in rows:
            if row.audience == row.Audience.AFFILIATE:
                farm_out = _manual_farm_out(trip) if farm_out is None else farm_out
                if not farm_out:
                    continue
            if (trip.pk, row.pk) in mine or (trip.pk, row.key) in legacy:
                continue
            if not rules.matches(row, lead, trip):
                continue
            created.append(_create(row, lead, trip, pickup + row.offset))
    return created


def schedule_dates(
    lead,
    reservation=None,
    *,
    only: MessageTemplate | None = None,
    master: bool | None = None,
    send_inline: bool = True,
) -> None:
    """(Re)ensure every date row's TouchPoint from its anchor. Idempotent.

    Trip-level rows for every trip of a booked order (only `reservation`, when given); the
    order's expiry and balance-due rows when no trip is given. Rows already due go out on
    commit, unless ``send_inline`` is False (the editor: the cron sends them). ``only``
    limits it to that one row (the editor queueing the row it saved), and ``master`` is the
    master switch already read, so a loop over orders reads it once.
    """
    if not (_master_on() if master is None else master):
        return
    created = []
    if reservation is None:
        created += _quote_expiry(lead, only=only)
        created += _balance_due(lead, only=only)
    if lead.status == lead.Status.BOOKED:
        trips = [reservation] if reservation is not None else list(lead.reservations.all())
        created += _pickup_rows(lead, trips, only=only)
    if send_inline:
        _send_on_commit(created)


def reschedule_trip(reservation) -> None:
    """A trip's pickup date or time changed.

    Its pickup rows are withdrawn, sent ones too, so a trip confirmed for the old date gets
    a fresh confirmation for the new one; the customer's and the affiliate's
    acknowledgements are for a date that no longer holds, so they are cleared; then the
    order's trips are rebuilt (as the old `reschedule_service_touchpoints`). Rows that
    wait for dispatch are re-dated by `sync_waiting`, not here."""
    lead = reservation.lead
    rows = MessageTemplate.objects.filter(trigger__in=_PICKUP_TRIGGERS)
    keys = list(rows.values_list("key", flat=True))
    TouchPoint.objects.filter(
        Q(message__in=rows) | Q(message__isnull=True, kind__in=keys),
        reservation=reservation,
        status__in=(_S.SCHEDULED, _S.SENT),
    ).update(status=_S.CANCELLED)

    if reservation.customer_confirmed_at is not None:
        reservation.customer_confirmed_at = None
        reservation.save(update_fields=["customer_confirmed_at", "updated_at"])
    confirmed = rules.confirmed_assignment(reservation)
    if confirmed is not None and confirmed.affiliate_confirmed_at is not None:
        confirmed.affiliate_confirmed_at = None
        confirmed.save(update_fields=["affiliate_confirmed_at", "updated_at"])

    if _master_on():
        _send_on_commit(_pickup_rows(lead, list(lead.reservations.all())))


# --- rows that wait --------------------------------------------------------------------

_WAITING_TRIGGERS = tuple(t.key for t in catalog.CATALOG.values() if t.waits_for)
# Rows whose sends carry the driver's details (`sender._DRIVER_TRIGGERS`), and the old
# scheduler's kinds for them.
_DRIVER_TRIGGERS = ("pickup_dispatched", "driver_changed")
_DRIVER_KINDS = ("driver_released", "driver_updated")


def _maybe_driver_changed(lead, reservation) -> None:
    """The driver details on file differ from the last ones the customer was sent: emit
    `driver_changed` (as the old `_maybe_update`). A send made before details were
    recorded is unknown and never counts as a change. A send that reached anyone counts,
    even one still holding its texts for quiet hours: its email went with the details it
    carries."""
    if not rules.ready(reservation, "dispatched_with_driver"):
        return
    rows = MessageTemplate.objects.filter(trigger__in=_DRIVER_TRIGGERS)
    reached = Delivery.objects.filter(touchpoint=OuterRef("pk"), status=Delivery.Status.SENT)
    sent = TouchPoint.objects.annotate(partly=Exists(reached)).filter(
        Q(message__in=rows) | Q(message__isnull=True, kind__in=_DRIVER_KINDS),
        Q(status=_S.SENT) | Q(status=_S.SCHEDULED, partly=True),
        reservation=reservation,
    )
    newest = max(sent, key=lambda tp: (tp.sent_at or tp.created_at, tp.pk), default=None)
    if newest is None or newest.driver_details is None:
        return
    if newest.driver_details == rules.confirmed_assignment(reservation).driver_info:
        return
    emit("driver_changed", lead, reservation)


def sync_waiting(
    reservation,
    *,
    only: MessageTemplate | None = None,
    master: bool | None = None,
    send_inline: bool = True,
) -> None:
    """Queue, or re-date, every row that waits for the trip to be Dispatched with its
    driver's details (as the old `sync_driver_message`).

    Called after anything that can change those facts: status, driver info, coverage,
    pickup. A row sends at `max(now, pickup + its timing)`: an early dispatch waits for the
    window, a late one goes out on commit. A queued row follows the pickup even while it is
    held; a sent one is left alone, and a change of driver after it emits `driver_changed`.
    Nothing happens once the pickup has passed. ``only`` limits it to that one row and skips
    the driver-change check (the editor queueing the row it saved); ``master`` is the master
    switch already read; ``send_inline`` False leaves what is due to the cron (the editor).
    """
    from apps.reservations.models import Reservation

    pickup = reservation.pickup_at
    now = timezone.now()
    if pickup is None or pickup <= now:
        return
    if reservation.trip_status not in rules.DRIVER_MESSAGE_STATUSES:
        # Not ready: only a queued (held) row has anything to do. Asked without touching
        # the message rows, so a status change costs nothing here (the common case).
        queued = set(
            TouchPoint.objects.filter(reservation=reservation, status=_S.SCHEDULED).values_list(
                "kind", flat=True
            )
        )
        if not queued:
            return
    else:
        queued = None
    rows = _rows_on(_WAITING_TRIGGERS, enabled_only=False, only=only)
    if not rows or (queued is not None and not queued & {r.key for r in rows}):
        return

    queued_now: list[TouchPoint] = []
    with transaction.atomic():
        # The webhook and a dispatcher can land together; the row lock serialises them so
        # the customer gets one message.
        locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
        lead = locked.lead
        readiness: dict[str, bool] = {}

        def ready(row: MessageTemplate) -> bool:
            waits_for = catalog.get(row.trigger).waits_for
            if waits_for not in readiness:
                readiness[waits_for] = rules.ready(locked, waits_for)
            return readiness[waits_for]

        any_sent = False
        for row in rows:
            existing = (
                TouchPoint.objects.filter(_of_row(row), reservation=locked)
                .exclude(status=_S.CANCELLED)
                .order_by("pk")
                .first()
            )
            if existing is not None and existing.status == _S.SENT:
                any_sent = True
                continue
            if existing is not None and existing.status == _S.SCHEDULED:
                # Partly sent: its email went while quiet hours hold its texts.
                any_sent = (
                    any_sent or existing.deliveries.filter(status=Delivery.Status.SENT).exists()
                )
            if existing is not None and existing.status != _S.SCHEDULED:
                continue
            at = max(now, pickup + row.offset)
            if existing is not None:
                if existing.updated_at > timezone.now():
                    # A sender holds its lease (`sender._claim`) and is sending it right
                    # now: leave it to that send. Saving it would end the lease and let a
                    # second claim send it again.
                    continue
                if existing.scheduled_for != at:
                    # One conditional update that leaves `updated_at` alone, so a lease
                    # taken since the read above still stands.
                    if not TouchPoint.objects.filter(
                        pk=existing.pk, status=_S.SCHEDULED, updated_at__lte=timezone.now()
                    ).update(scheduled_for=at):
                        continue  # claimed since: that send has it
                    existing.scheduled_for = at
                if ready(row):  # a held row that is ready now: catch up on commit
                    queued_now.append(existing)
                continue
            if not row.enabled:
                continue
            master = _master_on() if master is None else master
            if not master or not ready(row) or not rules.matches(row, lead, locked):
                continue
            queued_now.append(_create(row, lead, locked, at))
        if only is None and any_sent and locked.trip_status in rules.DRIVER_MESSAGE_STATUSES:
            _maybe_driver_changed(lead, locked)
    if send_inline:
        _send_on_commit(queued_now)


# --- withdrawing and re-timing ---------------------------------------------------------


def cancel(
    lead,
    *,
    triggers: Iterable[str] | None = None,
    reservation=None,
    audience: str | None = None,
) -> int:
    """Withdraw queued sends of the order; returns how many.

    By default the pre-booking program and outcome messages (`PRE_BOOKING`, today's
    `cancel_pending` default). Pass `triggers` (e.g. `EVERY` for a lost lead), and narrow
    to one trip with `reservation` or one audience with `audience` (dispatch releasing an
    affiliate cancels only the affiliate's confirmation)."""
    wanted = PRE_BOOKING if triggers is None else frozenset(triggers)
    rows = MessageTemplate.objects.filter(trigger__in=wanted)
    if audience is not None:
        rows = rows.filter(audience=audience)
    keys = list(rows.values_list("key", flat=True))
    qs = TouchPoint.objects.filter(lead=lead, status=_S.SCHEDULED).filter(
        Q(message__in=rows) | Q(message__isnull=True, kind__in=keys)
    )
    if reservation is not None:
        qs = qs.filter(reservation=reservation)
    return qs.update(status=_S.CANCELLED)


def withdraw(row: MessageTemplate, *, reason: str = "") -> int:
    """Cancel every queued send of a row, on every order: it was switched off, archived or
    moved to another trigger (spec section 9). ``reason`` goes on each as its error;
    `SWITCHED_OFF` lets the row's sends come back when it is switched on again. Returns
    how many."""
    changes = {"status": _S.CANCELLED}
    if reason:
        changes["error"] = reason
    return TouchPoint.objects.filter(_of_row(row), status=_S.SCHEDULED).update(**changes)


def redate(row: MessageTemplate, *, old_offset: timedelta) -> int:
    """Re-time every queued send of a row after its timing changed; returns the count.

    Trip date rows are recomputed from the trip's pickup (a row that waits never lands in
    the past); everything else moves by the change, its anchor being `scheduled_for -
    old_offset` (as spec B's re-dating did). A send whose texts are held for quiet hours
    (`sender._hold` moved its `scheduled_for` to the window's end) counts from its real
    anchor instead: the order's date, or the moment its event happened. Staff clicks and
    schedules have no queued sends."""
    trigger = catalog.CATALOG.get(row.trigger)
    if trigger is None or trigger.kind in ("staff_click", "schedule"):
        return 0
    new = row.offset
    qs = TouchPoint.objects.filter(_of_row(row), status=_S.SCHEDULED, scheduled_for__isnull=False)
    held = Delivery.objects.filter(touchpoint=OuterRef("pk"), status=Delivery.Status.QUEUED)
    changed = []
    if trigger.kind == "date" and trigger.level == "trip":
        now = timezone.now()
        for tp in qs.select_related("reservation"):
            pickup = tp.reservation.pickup_at if tp.reservation else None
            if pickup is None:
                continue
            at = pickup + new
            tp.scheduled_for = max(now, at) if trigger.waits_for else at
            changed.append(tp)
    else:
        for tp in qs.annotate(held=Exists(held)).select_related("lead__payment"):
            if not tp.held:
                tp.scheduled_for = tp.scheduled_for - old_offset + new
            elif trigger.anchor is not None:
                anchor = trigger.anchor(tp.lead, None)
                if anchor is None:
                    continue
                tp.scheduled_for = anchor + new
            else:
                tp.scheduled_for = _moment(trigger.key, tp.lead, tp.created_at) + new
            changed.append(tp)
    TouchPoint.objects.bulk_update(changed, ["scheduled_for"])
    return len(changed)
