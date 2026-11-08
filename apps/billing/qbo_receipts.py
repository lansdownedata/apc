"""APC-108: card payments pushed to QuickBooks as Sales Receipts and Refund Receipts.

Outbound only, on the APC-74 push machinery in `apps.payments.qbo_bills` (deterministic
requestids, leases, one alert per failure, SYNCED only when nothing changed mid-push).

* A card-paying contact becomes a QuickBooks **Customer** through its `CustomerLink`. A contact
  who owns a `BillingAccount` already in QuickBooks reuses that Customer and never gets a link —
  unless a link was pushed first, which then stays its customer (no second one is made).
* A name QuickBooks already has is taken over only when it is exactly one Customer, no other
  record here carries it, and it has the same email (with none on either side, the same phone
  or none): two people can share a name, and a wrong takeover would put one person's receipts
  on another's customer.
* Never call this inside an outer transaction — the client's token refresh is durable.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Exists, OuterRef, Prefetch, Q

from apps.billing.invoices import line_description
from apps.billing.models import AccountGroup, BillingAccount, CustomerLink, SyncState
from apps.billing.qbo_invoices import (
    DESCRIPTION_MAX,
    ITEM_LINE,
    NO_START_DATE,
    NO_TRIP_ITEM,
    customer_docs_from,
    trip_item_ref,
)
from apps.billing.qbo_sync import DUPLICATE_NAME, SYNC_BATCH, SyncPaused, _fault_code, current_realm
from apps.integrations import qbo
from apps.payments.qbo_bills import (
    _cents,
    _fail,
    _leased,
    _Link,
    _push,
    _quoted,
    _Refused,
    _same_total,
    _send,
    _stale,
    deposit_account_ref,
)

logger = logging.getLogger(__name__)

# QuickBooks' limit for a Customer's DisplayName.
DISPLAY_NAME_MAX = 100

# --- customers -----------------------------------------------------------------------------------


def _display_name(contact) -> str:
    return contact.name.strip()[:DISPLAY_NAME_MAX]


def _linked_here(link: CustomerLink | None, realm: str) -> bool:
    return bool(link and link.qbo_id and link.qbo_realm_id == realm)


def _link_of(contact) -> CustomerLink | None:
    """Read through the relation so a select_related `qbo_customer` costs no query."""
    try:
        return contact.qbo_customer
    except CustomerLink.DoesNotExist:
        return None


def _account_customer(contact, realm: str) -> BillingAccount | None:
    """The contact's billing account (one at most), when its Customer is in this company.
    Read through `.all()` so prefetched accounts cost no query."""
    for account in contact.billing_accounts.all():
        if account.qbo_id and account.qbo_realm_id == realm:
            return account
    return None


def customer_ref(contact, realm: str) -> dict | None:
    """The QuickBooks Customer a contact's card receipts go to in this company, or None until
    one is there. A pushed link wins over a billing account that came later: the receipts
    already on it stay with the customer they were made for."""
    link = _link_of(contact)
    if _linked_here(link, realm):
        return {"value": link.qbo_id}
    account = _account_customer(contact, realm)
    if account is not None:
        return {"value": account.qbo_id}
    return None


def ensure_link(contact) -> CustomerLink | None:
    """The contact's link, made if it needs one. None (and nothing made) when its billing
    account's Customer is in this company and it has no link of its own."""
    link = _link_of(contact)
    if link is not None:
        return link
    if _account_customer(contact, current_realm()) is not None:
        return None
    link, _ = CustomerLink.objects.get_or_create(
        contact=contact, defaults={"display_name": _display_name(contact)}
    )
    return link


def customer_payload(link: CustomerLink) -> dict:
    contact = link.contact
    body = {"DisplayName": link.display_name}
    # On an update an empty value clears the old one; leaving the key out would keep a
    # removed address or number in QuickBooks.
    if contact.email or link.qbo_id:
        body["PrimaryEmailAddr"] = {"Address": contact.email or ""}
    if contact.phone or link.qbo_id:
        body["PrimaryPhone"] = {"FreeFormNumber": contact.phone or ""}
    return body


def _takeover_payload(link: CustomerLink) -> dict:
    """Taking over a customer the bookkeeper already made: our name and only what we actually
    have — never a blank over their email or phone."""
    contact = link.contact
    body = {"DisplayName": link.display_name}
    if contact.email:
        body["PrimaryEmailAddr"] = {"Address": contact.email}
    if contact.phone:
        body["PrimaryPhone"] = {"FreeFormNumber": contact.phone}
    return body


def _email(value) -> str:
    return (value or "").strip().lower()


def _digits(value) -> str:
    return re.sub(r"\D", "", value or "")


def _held(found: dict, key: str, field: str) -> str:
    value = found.get(key)
    return (value.get(field) if isinstance(value, dict) else "") or ""


def _same_person(found: dict, contact) -> bool:
    """The Customer QuickBooks holds under this name is our contact: the same email, ignoring
    case. With no email on either side, the same phone digits, or no phone on either side."""
    theirs, ours = _email(_held(found, "PrimaryEmailAddr", "Address")), _email(contact.email)
    if theirs or ours:
        return theirs == ours
    return _digits(_held(found, "PrimaryPhone", "FreeFormNumber")) == _digits(contact.phone)


def _held_here(qbo_id: str, realm: str, link: CustomerLink) -> bool:
    """Another record in this company already carries this Customer: a second link, a billing
    account or a group. Taking it over would merge two of our customers into one."""
    mine = {"qbo_id": qbo_id, "qbo_realm_id": realm}
    return (
        CustomerLink.objects.filter(**mine).exclude(pk=link.pk).exists()
        or BillingAccount.objects.filter(**mine).exists()
        or AccountGroup.objects.filter(**mine).exists()
    )


def _take_over_customer(link: _Link, exc: qbo.QboAPIError, body: dict) -> dict:
    """QuickBooks already has this name. If it is exactly one Customer and the same person
    (`_same_person`), link it and send ours over it; otherwise it's for a person to sort out —
    never a guess that puts one customer's receipts on another's."""
    if link.get("id") or _fault_code(exc) != DUPLICATE_NAME:
        raise exc
    record: CustomerLink = link.record
    query = f"select * from Customer where DisplayName = {_quoted(record.display_name)}"
    rows = qbo.request("GET", "query", params={"query": query})
    found = rows.get("QueryResponse", {}).get("Customer", [])
    if (
        len(found) != 1
        or not _same_person(found[0], record.contact)
        or _held_here(str(found[0]["Id"]), current_realm(), record)
    ):
        raise _Refused(
            f"QuickBooks already has a name like “{record.display_name}” (it may differ in"
            " case or spacing). Rename the customer here or there, then retry."
        )
    link.put("id", str(found[0]["Id"]))
    link.put("sync_token", str(found[0]["SyncToken"]))
    return _send(link, _takeover_payload(record))


def _push_customer(record: CustomerLink) -> bool:
    link = _Link(record, "customer", "Customer", sparse=True)
    return _push(
        link,
        lambda: customer_payload(record),
        matches=lambda obj, body: (
            obj.get("DisplayName", body["DisplayName"]) == body["DisplayName"]
        ),
        what="customer",
        on_refusal=lambda exc, body: _take_over_customer(link, exc, body),
    )


def push_customer(link: CustomerLink) -> bool:
    """Create, take over or update the contact's QuickBooks Customer, holding the link's lease.
    True when it is now in step; False (no call) while another push has it, or while the
    contact's billing account is its Customer here. Raises SyncPaused / QboNotConnected."""
    with _leased(CustomerLink, link.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        link.refresh_from_db()
        realm = current_realm()
        if not _linked_here(link, realm) and _account_customer(link.contact, realm):
            return False  # the billing account's Customer is this contact's: none of its own
        return _push_customer(link)


def pending_links(realm: str):
    """Links not in step with this company whose contact has a card receipt to carry: a
    succeeded trip charge or refund on a processed trip. One not pushed here whose contact's
    billing account is in this company is left out — that account is its customer."""
    from apps.payments.models import Charge

    candidates = Charge.objects.filter(
        status=Charge.Status.SUCCEEDED,
        kind__in=(Charge.Kind.TRIP, Charge.Kind.REFUND),
        reservation__isnull=False,
        reservation__review__processed_at__isnull=False,
        reservation__lead__contact=OuterRef("contact"),
    )
    accounts = BillingAccount.objects.filter(contact=OuterRef("contact"), qbo_realm_id=realm)
    linked_here = ~Q(qbo_id="") & Q(qbo_realm_id=realm)
    return (
        CustomerLink.objects.filter(_stale("qbo_", realm))
        .filter(Exists(candidates))
        .filter(linked_here | ~Exists(accounts.exclude(qbo_id="")))
        .select_related("contact")
        .order_by("pk")
    )


# --- payloads ------------------------------------------------------------------------------------

NO_DEPOSIT_ACCOUNT = (
    "Waiting: choose the deposit account for card payments in Settings \u2192 QuickBooks."
)
WAITING_FOR_CUSTOMER = "Waiting for the customer in QuickBooks."
# QuickBooks' limit for PaymentRefNum.
PAYMENT_REF_MAX = 21


def _trip_zone(trip) -> ZoneInfo:
    """The trip's own zone. One that isn't a real zone refuses this record, not the run."""
    name = trip.pickup_timezone or "America/New_York"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise _Refused(f"This trip's timezone isn't valid: {name}.") from None


def txn_date(charge) -> date:
    """The receipt's date: when the charge was made, on the calendar of the trip's own zone."""
    return charge.created_at.astimezone(_trip_zone(charge.reservation)).date()


def _receipt_base(charge, realm: str, ref_number: str) -> tuple[dict, dict, str]:
    """The keys both receipts share, the item ref and the trip reference. Raises `_Refused`
    (no alert: the cause is shared by every receipt) until the item, the deposit account and
    the customer are all in QuickBooks."""
    item = trip_item_ref()
    if item is None:
        raise _Refused(NO_TRIP_ITEM, alert=False)
    deposit = deposit_account_ref()
    if deposit is None:
        raise _Refused(NO_DEPOSIT_ACCOUNT, alert=False)
    trip = charge.reservation
    customer = customer_ref(trip.lead.contact, realm)
    if customer is None:
        raise _Refused(WAITING_FOR_CUSTOMER, alert=False)
    body = {
        "CustomerRef": customer,
        "TxnDate": txn_date(charge).isoformat(),
        "DepositToAccountRef": deposit,
    }
    if ref_number:
        body["PaymentRefNum"] = ref_number[:PAYMENT_REF_MAX]
    body["PrivateNote"] = (
        f"{trip.reference} \u00b7 order {trip.lead.quote_no} \u00b7 receipt R-{charge.pk}"
    )
    return body, item, trip.reference


def _item_line(charge, item: dict, description: str) -> dict:
    amount = f"{charge.amount:.2f}"
    return {
        "DetailType": ITEM_LINE,
        "Amount": amount,
        "Description": description[:DESCRIPTION_MAX],
        "SalesItemLineDetail": {"ItemRef": item, "Qty": 1, "UnitPrice": amount},
    }


def sales_receipt_payload(charge, realm: str) -> dict:
    """The QuickBooks Sales Receipt body for a succeeded card charge. No PaymentMethodRef and
    no DocNumber: QuickBooks numbers it, and the receipt id rides on the note and line."""
    body, item, _ = _receipt_base(charge, realm, charge.stripe_payment_intent_id)
    card = f" \u00b7 card \u2022\u2022{charge.card_last4}" if charge.card_last4 else ""
    description = f"{line_description(charge.reservation)}{card} \u00b7 receipt R-{charge.pk}"
    body["Line"] = [_item_line(charge, item, description)]
    return body


def refund_receipt_payload(charge, realm: str) -> dict:
    """The QuickBooks Refund Receipt body for a succeeded card refund."""
    body, item, reference = _receipt_base(charge, realm, charge.stripe_refund_id)
    description = f"Refund \u00b7 {charge.note or 'Refund'} \u00b7 {reference}"
    body["Line"] = [_item_line(charge, item, description)]
    return body


def receipt_matches(obj: dict, body: dict) -> bool:
    """What QuickBooks holds is what we sent: the same total and exactly one item line."""
    lines = [row for row in obj.get("Line", []) if row.get("DetailType") == ITEM_LINE]
    return len(lines) == 1 and _same_total(obj, Decimal(body["Line"][0]["Amount"]))


# --- the push ------------------------------------------------------------------------------------


def _refresh(charge) -> None:
    """Re-read the charge's own fields before a push, never its relations, so the trip,
    order, contact and stops the run loaded once stay loaded."""
    fields = [f.attname for f in type(charge)._meta.concrete_fields if not f.is_relation]
    charge.refresh_from_db(fields=fields)


def _receipts(realm: str, *, linked: bool = False):
    """Charges that are card receipts not in step with this company: a succeeded trip charge,
    or a card refund (blank on older rows = the card), on a processed trip. Deposits, balances
    and final bills never go. One already in this company is left out unless `linked`: the
    portal never edits a receipt, so only a person's Retry sends one again."""
    from apps.payments.models import Charge

    card_refund = Q(kind=Charge.Kind.REFUND, refund_method__in=("", Charge.RefundMethod.CARD))
    rows = (
        Charge.objects.filter(
            status=Charge.Status.SUCCEEDED,
            reservation__isnull=False,
            reservation__review__processed_at__isnull=False,
        )
        .filter(Q(kind=Charge.Kind.TRIP) | card_refund)
        .filter(_stale("qbo_", realm))
    )
    if not linked:
        rows = rows.exclude(~Q(qbo_id="") & Q(qbo_realm_id=realm))
    return rows


def _processed_on(charge) -> date | None:
    """The trip's processed date on its own calendar (the manifest rule)."""
    review = getattr(charge.reservation, "review", None) if charge.reservation_id else None
    if review is None or review.processed_at is None:
        return None
    return review.processed_at.astimezone(_trip_zone(charge.reservation)).date()


def _in_window(charge, start: date) -> bool:
    """Processed on or after `start`. A trip whose zone isn't valid is in, so its push says
    so on the row."""
    try:
        processed = _processed_on(charge)
    except _Refused:
        return True
    return processed is not None and processed >= start


def pending_receipts(realm: str, start: date | None, *, linked: bool = False) -> list:
    """Card receipts to send, in pk order, each loaded with everything its payload reads (no
    query per charge). Those processed before `start` are never sent and say nothing; with no
    start date (None) every one comes back, for the run to mark as waiting on it."""
    from apps.reservations.models import Stop

    rows = _receipts(realm, linked=linked)
    if start is not None:
        # A coarse SQL cut (no zone is a day from UTC); `_in_window` decides.
        cut = datetime.combine(start - timedelta(days=1), time.min, tzinfo=UTC)
        rows = rows.filter(reservation__review__processed_at__gte=cut)
    rows = (
        rows.select_related(
            "reservation__vehicle",
            "reservation__lead__contact__qbo_customer",
            "reservation__review",
        )
        .prefetch_related(
            Prefetch("reservation__stops", queryset=Stop.objects.order_by("sequence")),
            "reservation__lead__contact__billing_accounts",
        )
        .order_by("pk")
    )
    return [row for row in rows if start is None or _in_window(row, start)]


def _contact(charge):
    return charge.reservation.lead.contact


def _in_step(charge, realm: str) -> bool:
    return (
        bool(charge.qbo_id)
        and charge.qbo_realm_id == realm
        and charge.qbo_sync_state == SyncState.SYNCED
    )


def _mismatch(obj: dict, body: dict, charge, what: str) -> str:
    """Why what QuickBooks holds isn't what we sent, in a sentence staff can act on."""
    if not _same_total(obj, charge.amount):
        held = _cents(obj.get("TotalAmt"))
        if held is not None:
            return (
                f"QuickBooks totals this {what} ${held:,.2f}, not ${charge.amount:,.2f}. "
                "Check sales tax on the Product/Service."
            )
    items = sum(1 for line in obj.get("Line", []) if line.get("DetailType") == ITEM_LINE)
    return (
        f"QuickBooks holds this {what} with {items} lines, not {len(body['Line'])}. "
        "Check it in QuickBooks."
    )


def _receipt_link(charge) -> tuple[_Link, str, object]:
    """The charge's QuickBooks object, what staff call it, and its payload builder."""
    from apps.payments.models import Charge

    if charge.kind == Charge.Kind.TRIP:
        return _Link(charge, "salesreceipt", "SalesReceipt"), "receipt", sales_receipt_payload
    return _Link(charge, "refundreceipt", "RefundReceipt"), "refund receipt", refund_receipt_payload


def _push_receipt(charge, realm: str) -> bool:
    """Create (or, on a Retry, update) the receipt. One QuickBooks holds differently is ERROR
    naming both figures and alerts once; the hourly run never sends it again."""
    link, what, build = _receipt_link(charge)
    mismatch = ""
    already_failed = charge.qbo_sync_state == SyncState.ERROR

    def matches(obj: dict, body: dict) -> bool:
        nonlocal mismatch
        if receipt_matches(obj, body):
            return True
        mismatch = _mismatch(obj, body, charge, what)
        return False

    in_step = _push(link, lambda: build(charge, realm), matches=matches, what=what)
    if mismatch and charge.qbo_id and charge.qbo_sync_state == SyncState.NOT_SYNCED:
        _fail(link, mismatch, alert=not already_failed)
    return in_step


def push_receipt(charge) -> bool:
    """Send one card receipt to QuickBooks, holding the charge's lease. True when it is now in
    step; False with no call while another push has it, while it isn't a receipt to send
    (a deposit, an offline refund, processed before the start date) or while its customer
    isn't in QuickBooks yet. Raises SyncPaused / QboNotConnected."""
    from apps.payments.models import Charge

    with _leased(Charge, charge.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        realm = current_realm()
        start = customer_docs_from()
        _clear_blocks(start, Charge.objects.filter(pk=charge.pk))
        _refresh(charge)
        if not _receipts(realm, linked=True).filter(pk=charge.pk).exists():
            return _in_step(charge, realm)
        if start is None:
            _fail(_receipt_link(charge)[0], NO_START_DATE, alert=False)
            return False
        if not _in_window(charge, start):
            return False
        if customer_ref(_contact(charge), realm) is None:
            return False  # waits for its customer, with nothing said
        return _push_receipt(charge, realm)


# --- hourly --------------------------------------------------------------------------------------


def _clear_blocks(start: date | None, rows=None) -> None:
    """A block whose setting has since been chosen no longer applies: back to not sent, so a
    charge now before the start date says nothing and the next real refusal alerts."""
    from apps.payments.models import Charge

    cleared = []
    if start is not None:
        cleared.append(NO_START_DATE)
    if trip_item_ref() is not None:
        cleared.append(NO_TRIP_ITEM)
    if deposit_account_ref() is not None:
        cleared.append(NO_DEPOSIT_ACCOUNT)
    if not cleared:
        return
    rows = Charge.objects.all() if rows is None else rows
    rows.filter(qbo_sync_state=SyncState.ERROR, qbo_sync_error__in=cleared).update(
        qbo_sync_state=SyncState.NOT_SYNCED, qbo_sync_error=""
    )


def _block_all(rows: list, message: str) -> bool:
    """Every pending receipt shares one missing setting: mark each (so the row says why). True
    when any is newly marked, so the run alerts once rather than every hour."""
    from apps.payments.models import Charge

    fresh = [
        row
        for row in rows
        if row.qbo_sync_state != SyncState.ERROR or row.qbo_sync_error != message
    ]
    if fresh:
        Charge.objects.filter(pk__in=[row.pk for row in rows]).update(
            qbo_sync_state=SyncState.ERROR, qbo_sync_error=message
        )
    return bool(fresh)


def _alert_blocked(message: str) -> None:
    from apps.notifications.models import Notification

    Notification.notify(
        None,
        Notification.Kind.SYNC_FAILED,
        title="Card receipts can't go to QuickBooks yet",
        detail=message,
    )


def _block(rows: list, start: date | None) -> str:
    """The missing setting every pending receipt waits on, newly marked ("" when none or
    already marked); rows are marked whenever one is missing."""
    for missing, message in (
        (start is None, NO_START_DATE),
        (trip_item_ref() is None, NO_TRIP_ITEM),
        (deposit_account_ref() is None, NO_DEPOSIT_ACCOUNT),
    ):
        if missing:
            return message if _block_all(rows, message) else ""
    return ""


def _blocked(start: date | None) -> bool:
    return start is None or trip_item_ref() is None or deposit_account_ref() is None


def _push_customers(rows: list, realm: str) -> int:
    """Give every contact here with no usable customer a link (one per contact), then push up
    to SYNC_BATCH links. Returns how many landed."""
    waiting = {}
    for charge in rows:
        contact = _contact(charge)
        if contact.pk not in waiting and customer_ref(contact, realm) is None:
            waiting[contact.pk] = contact
    for contact in waiting.values():
        ensure_link(contact)
    return sum(push_customer(link) for link in pending_links(realm)[:SYNC_BATCH])


def sync_pending() -> int:
    """Push card customers, then up to SYNC_BATCH card receipts in pk order. Returns how many
    landed. A missing setting marks every pending receipt and alerts once. Stops quietly on an
    outage, a rate limit or a lost connection; nothing is marked."""
    if not qbo.is_connected():
        return 0
    realm = current_realm()
    start = customer_docs_from()
    pushed = attempted = 0
    blocked = ""  # newly blocked by a missing setting: alert once for the run
    _clear_blocks(start)
    try:
        rows = pending_receipts(realm, start)
        if rows and _blocked(start):
            blocked = _block(rows, start)
            return pushed
        pushed += _push_customers(rows, realm)
        # Re-read: a customer pushed just now lets its receipts go in the same run.
        for charge in pending_receipts(realm, start):
            if customer_ref(_contact(charge), realm) is None:
                continue  # waits for its customer
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push_receipt(charge)
    except (SyncPaused, qbo.QboNotConnected) as exc:
        logger.warning("QuickBooks receipt sync paused: %s", exc)
    finally:
        if blocked:
            _alert_blocked(blocked)
    return pushed


_UNSET = object()


def sync_state(charge, *, connected: bool | None = None, start=_UNSET) -> dict | None:
    """What the charge row shows: its state, its sentence, where Retry posts and whether its
    trip was processed before the start date (so never sent). None while QuickBooks isn't
    connected. Pass `connected` and `start` when building many."""
    from django.urls import reverse

    if connected is None:
        connected = qbo.is_connected()
    if not connected:
        return None
    if start is _UNSET:
        start = customer_docs_from()
    before = False
    if start is not None:
        try:
            processed = _processed_on(charge)
        except _Refused:
            processed = None
        before = processed is not None and processed < start
    return {
        "state": charge.qbo_sync_state,
        "error": charge.qbo_sync_error,
        "sync_url": reverse("charge_qbo_sync", args=[charge.pk]),
        "before_start": before,
    }
