"""Push affiliate bills and bill payments to QuickBooks Online (APC-74) — outbound only.

The portal is the system of record: nobody enters or pays affiliate bills in QuickBooks, and
nothing is read back except the company's chart of accounts (a cache, for the two choices on
Settings → QuickBooks).

* `vendors.Vendor` → a QuickBooks **Vendor**, pushed once it has a bill to carry. A name
  QuickBooks already has as a vendor is linked, not duplicated ("created or linked on first
  sync"); a name held by a customer or employee is a readable error.
* An approved `AffiliatePayable` → a **Bill** (the mixin's `qbo_*` fields), with its invoice
  file as an **Attachable**; marked paid → a **BillPayment** (the `qbo_payment_*` fields).
* The same rules as APC-42's customer sync (`billing.qbo_sync`), whose helpers this reuses:
  nothing is pushed from the request that saves a record (approve / mark paid / an edit mark
  it NOT_SYNCED; the hourly `retry-qbo-sync` job or a person's Retry pushes it); a
  deterministic `requestid` per record and change so a retry never duplicates; an outage or
  rate limit stops the run and marks nothing; only a definite refusal marks ERROR, with one
  alert; SYNCED only if nobody edited the record while the push was in flight; a link to
  another QuickBooks company is pushed again as new.
* A dependent waits for its parent's QuickBooks id without erroring: a Bill for its Vendor, a
  BillPayment for its Bill.
* Never pushes the portal's own journal entries: QuickBooks' Bill and BillPayment make its
  side of the accounting, and "Affiliate disbursements" has no QuickBooks counterpart.
* Never call this inside an outer transaction — the client's token refresh is durable.

API facts and what is still unverified: docs/superpowers/specs/2026-09-19-billing-accounts/
qbo-notes.md (the APC-74 section).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.billing import qbo_sync as customers
from apps.billing.models import QboAccount, QboAccountMapping, SyncState
from apps.billing.qbo_sync import (
    DUPLICATE_NAME,
    STALE_OBJECT,
    SYNC_BATCH,
    SyncPaused,
    _digest,
    _errors,
    _fault_code,
    _transient,
)
from apps.integrations import qbo

from .models import AffiliatePayable

logger = logging.getLogger(__name__)

# Intuit: DocNumber is at most 21 characters in QuickBooks Online (Finance.xsd).
DOC_NUMBER_MAX = 21
ZERO = Decimal("0.00")
_NO_EXPENSE_ACCOUNT = (
    "Choose the expense account for affiliate bills in Settings → QuickBooks, then retry."
)
_NO_PAYMENT_ACCOUNT = (
    "Choose the bank account bill payments come from in Settings → QuickBooks, then retry."
)


# --- the chart of accounts cache and the two choices ------------------------------------------


def refresh_accounts() -> int:
    """Cache the company's expense and bank accounts; one no longer there is retired, not
    deleted (a mapping that pointed at it then reads as unset). Returns how many are active."""
    realm = customers.current_realm()
    kinds = (*QboAccount.EXPENSE_TYPES, *QboAccount.PAYMENT_TYPES)
    rows = qbo.request("GET", "query", params={"query": "select * from Account maxresults 1000"})
    seen = []
    for row in rows.get("QueryResponse", {}).get("Account", []):
        if row.get("AccountType") not in kinds:
            continue
        QboAccount.objects.update_or_create(
            realm_id=realm,
            qbo_id=str(row["Id"]),
            defaults={
                "name": (row.get("FullyQualifiedName") or row.get("Name") or "")[:255],
                "account_type": row["AccountType"],
                "active": bool(row.get("Active", True)),
            },
        )
        seen.append(str(row["Id"]))
    QboAccount.objects.exclude(realm_id=realm, qbo_id__in=seen).update(active=False)
    return QboAccount.objects.filter(realm_id=realm, active=True).count()


def _usable(types: tuple[str, ...]):
    return QboAccount.objects.filter(
        realm_id=customers.current_realm(), active=True, account_type__in=types
    )


def expense_choices() -> list[tuple[int, str]]:
    return list(_usable(QboAccount.EXPENSE_TYPES).values_list("pk", "name"))


def payment_choices() -> list[tuple[int, str]]:
    return list(_usable(QboAccount.PAYMENT_TYPES).values_list("pk", "name"))


def _ref(account: QboAccount | None, types: tuple[str, ...]) -> dict | None:
    if account is None or not _usable(types).filter(pk=account.pk).exists():
        return None
    return {"value": account.qbo_id}


def expense_account_ref() -> dict | None:
    """Where a bill's lines post, or None until one is chosen for this company."""
    return _ref(QboAccountMapping.load().expense_account, QboAccount.EXPENSE_TYPES)


def payment_account_ref() -> dict | None:
    """The bank account bill payments come from, or None until one is chosen."""
    return _ref(QboAccountMapping.load().payment_account, QboAccount.PAYMENT_TYPES)


# --- the shared push machinery -------------------------------------------------------------------


class _Refused(Exception):
    """A definite problem with this record (shown to staff); not a reason to stop the run.
    `alert=False` when the cause is shared by every record (a missing account choice) and the
    run alerts once instead."""

    def __init__(self, message: str, *, alert: bool = True, by_quickbooks: bool = False):
        super().__init__(message)
        self.alert = alert
        # QuickBooks itself said no (a refused file inside a 200): the next try is a new create.
        self.by_quickbooks = by_quickbooks


_TAGS = {"vendor": "v", "bill": "b", "billpayment": "p", "attachment": "f"}


@dataclass
class _Link:
    """One QuickBooks object a portal record carries: its fields share a prefix, so a payable
    carries two (its Bill under `qbo_`, its BillPayment under `qbo_payment_`)."""

    record: object
    entity: str  # the API path: "vendor" · "bill" · "billpayment"
    key: str  # the response key: "Vendor" · "Bill" · "BillPayment"
    prefix: str = "qbo_"
    sparse: bool = False  # Intuit documents sparse updates for Vendor; Bill/BillPayment go whole

    def get(self, field: str):
        return getattr(self.record, self.prefix + field)

    def put(self, field: str, value) -> None:
        setattr(self.record, self.prefix + field, value)

    def field(self, name: str) -> str:
        return self.prefix + name

    @property
    def tag(self) -> str:
        """Which record, in a requestid: v12 (vendor), b12 (bill), p12 (payment)."""
        return f"{_TAGS[self.entity]}{self.record.pk}"

    def update(self, **fields) -> int:
        model = type(self.record)
        return model.objects.filter(pk=self.record.pk).update(
            **{self.field(k): v for k, v in fields.items()}
        )


# A push holds its record's lease while it talks to Intuit, so the cron and a Retry (or a
# double-click) never send the same vendor, bill, file or payment at once. Taken by a
# conditional UPDATE, never a transaction around the HTTP call; a lease older than this is a
# push that died and is taken over.
LEASE = timedelta(minutes=10)


@contextmanager
def _leased(model, pk: int, field: str):
    """Yields True when this caller holds `field`'s lease on the row, False when another push
    has it. Released on the way out (only our own lease, by its timestamp)."""
    now = timezone.now()
    free = Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__lt": now - LEASE})
    mine = bool(model.objects.filter(pk=pk).filter(free).update(**{field: now}))
    try:
        yield mine
    finally:
        if mine:
            model.objects.filter(pk=pk, **{field: now}).update(**{field: None})


def being_sent(record, field: str) -> bool:
    started = getattr(record, field)
    return bool(started and started >= timezone.now() - LEASE)


def _request_id(link: _Link, body: dict) -> str:
    """Deterministic per record and change, as in APC-42: a create keeps its id until
    QuickBooks itself refuses one (`create_attempt`), an update is keyed by its body and the
    SyncToken it was made against."""
    if not link.get("id"):
        return customers.create_request_id(link.tag, link.get("create_attempt"))
    return f"apc-{_digest(body)}-{link.tag}-u{link.get('sync_token')}"[:50]


def _send(link: _Link, body: dict) -> dict:
    if link.get("id"):
        body = {**body, "Id": link.get("id"), "SyncToken": link.get("sync_token")}
        if link.sparse:
            body["sparse"] = True
    return qbo.request(
        "POST", link.entity, params={"requestid": _request_id(link, body)}, json=body
    )[link.key]


def _send_or_refresh_token(link: _Link, body: dict) -> dict:
    try:
        return _send(link, body)
    except qbo.QboAPIError as exc:
        if not (link.get("id") and _fault_code(exc) == STALE_OBJECT):
            raise
    # Changed in QuickBooks since our last push: take its current SyncToken, retry once.
    current = qbo.request("GET", f"{link.entity}/{link.get('id')}")[link.key]
    link.put("sync_token", str(current["SyncToken"]))
    return _send(link, body)


def _reason(exc: qbo.QboAPIError, what: str, *, article: str = "this") -> str:
    errors = _errors(exc)
    reason = (errors[0].get("Detail") or errors[0].get("Message")) if errors else ""
    if reason:
        return f"QuickBooks didn't accept {article} {what}: {reason}"
    return f"QuickBooks didn't accept {article} {what} ({exc.status}). Check it, then retry."


def _alert(link: _Link, message: str) -> None:
    from apps.notifications.models import Notification

    record = link.record
    if link.entity == "vendor":
        Notification.objects.create(
            vendor=record,
            kind=Notification.Kind.SYNC_FAILED,
            title=f"QuickBooks sync failed: {record.name}",
            detail=message,
        )
        return
    what = {"bill": "bill", "billpayment": "bill payment", "attachment": "invoice file"}[
        link.entity
    ]
    Notification.notify(
        record.assignment.reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=f"QuickBooks {what} failed: {_who(record)}",
        detail=message,
    )


def _who(payable: AffiliatePayable) -> str:
    number = f" invoice {payable.invoice_number}" if payable.invoice_number else ""
    return f"{payable.assignment.vendor.name}{number}"


def _fail(link: _Link, message: str, *, alert: bool = True) -> bool:
    """Mark the link ERROR with a sentence staff can act on. Alerts on the first failure only,
    not on every hourly retry. Returns whether it was newly failed."""
    first_failure = link.get("sync_state") != SyncState.ERROR
    link.put("sync_state", SyncState.ERROR)
    link.put("sync_error", message[:255])
    link.update(sync_state=SyncState.ERROR, sync_error=link.get("sync_error"))
    if first_failure and alert:
        _alert(link, link.get("sync_error"))
    return first_failure


def _relink_if_other_company(link: _Link, realm: str) -> bool:
    """A link to another QuickBooks company is pushed to this one as new."""
    if link.get("id") and link.get("realm_id") and link.get("realm_id") != realm:
        link.put("id", "")
        link.put("sync_token", "")
        return True
    return False


def _push(link: _Link, build, *, matches, what: str, on_refusal=None) -> bool:
    """Create or update one QuickBooks object. True when it is now in step.

    `build()` returns the body (or raises _Refused); `matches(obj, body)` says whether what came
    back is what was sent;
    `on_refusal(exc, body)` may answer a refusal itself (the vendor's link-by-name).
    Raises SyncPaused on an outage or rate limit and QboNotConnected without a connection.
    """
    realm = customers.current_realm()
    _relink_if_other_company(link, realm)
    snapshot = link.record.updated_at
    try:
        body = build()
        try:
            obj = _send_or_refresh_token(link, body)
        except qbo.QboAPIError as exc:
            if _transient(exc) or on_refusal is None:
                raise
            obj = on_refusal(exc, body)
        link.update(
            id=str(obj["Id"]),
            sync_token=str(obj["SyncToken"]),
            realm_id=realm,
            synced_at=timezone.now(),
        )
        link.put("id", str(obj["Id"]))
        link.put("sync_token", str(obj["SyncToken"]))
        link.put("realm_id", realm)
    except _Refused as exc:
        _fail(link, str(exc), alert=exc.alert)
        return False
    except qbo.QboAPIError as exc:
        logger.warning("QuickBooks refused %s %s: %s", link.entity, link.record.pk, exc.body[:500])
        if _transient(exc):
            raise SyncPaused(str(exc)) from exc
        customers.refused_by_quickbooks(link.record, link.prefix)
        _fail(link, _reason(exc, what))
        return False
    model = type(link.record)
    # SYNCED only if nobody edited the record while the push was in flight and QuickBooks
    # returned what we sent.
    if matches(obj, body):
        model.objects.filter(pk=link.record.pk, updated_at=snapshot).update(
            **{link.field("sync_state"): SyncState.SYNCED, link.field("sync_error"): ""}
        )
    else:
        link.update(sync_state=SyncState.NOT_SYNCED)
    link.record.refresh_from_db()
    return link.get("sync_state") == SyncState.SYNCED


def _ref_value(obj: dict, key: str):
    ref = obj.get(key)
    return ref.get("value") if isinstance(ref, dict) else None


# --- vendors ------------------------------------------------------------------------------------


def vendor_payload(vendor) -> dict:
    body = {"DisplayName": vendor.name, "CompanyName": vendor.name}
    # On an update an empty value clears the old one; leaving the key out would keep a
    # removed address or number in QuickBooks.
    if vendor.email or vendor.qbo_id:
        body["PrimaryEmailAddr"] = {"Address": vendor.email}
    if vendor.phone or vendor.qbo_id:
        body["PrimaryPhone"] = {"FreeFormNumber": vendor.phone}
    return body


def _quoted(value: str) -> str:
    """A string literal for Intuit's query language: apostrophes backslash-escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _link_payload(vendor) -> dict:
    """Taking over a vendor the bookkeeper already made: send the name and only what we
    actually have — never a blank over their email or phone, never our name over their
    company name."""
    body = {"DisplayName": vendor.name}
    if vendor.email:
        body["PrimaryEmailAddr"] = {"Address": vendor.email}
    if vendor.phone:
        body["PrimaryPhone"] = {"FreeFormNumber": vendor.phone}
    return body


def _link_existing_vendor(link: _Link, exc: qbo.QboAPIError, body: dict) -> dict:
    """QuickBooks already has this name. If it is exactly one vendor, link it and send ours
    over it ("created or linked on first sync"); otherwise it's for a person to sort out."""
    if link.get("id") or _fault_code(exc) != DUPLICATE_NAME:
        raise exc
    vendor = link.record
    query = f"select * from Vendor where DisplayName = {_quoted(vendor.name)}"
    rows = qbo.request("GET", "query", params={"query": query})
    found = rows.get("QueryResponse", {}).get("Vendor", [])
    if len(found) != 1:
        raise _Refused(
            f"QuickBooks already has a name like “{vendor.name}” (it may differ in case or"
            " spacing). Rename the vendor here or there, then retry."
        )
    link.put("id", str(found[0]["Id"]))
    link.put("sync_token", str(found[0]["SyncToken"]))
    return _send(link, _link_payload(vendor))


def _push_vendor(vendor) -> bool:
    """Create, link or update the vendor in QuickBooks. True when it is now in step."""
    link = _Link(vendor, "vendor", "Vendor", sparse=True)
    return _push(
        link,
        lambda: vendor_payload(vendor),
        matches=lambda obj, body: (
            obj.get("DisplayName", body["DisplayName"]) == body["DisplayName"]
        ),
        what="vendor",
        on_refusal=lambda exc, body: _link_existing_vendor(link, exc, body),
    )


def _in_company(record, realm: str, prefix: str = "qbo_") -> bool:
    return bool(getattr(record, prefix + "id")) and getattr(record, prefix + "realm_id") in (
        realm,
        "",
    )


# --- bills ---------------------------------------------------------------------------------------


def _trip_date(moment, trip):
    """A date as the trip's own timezone has it (the manifest rule), never UTC's."""
    zone = ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)
    return timezone.localtime(moment or timezone.now(), zone).date()


def _customer_ref(lead, realm: str) -> dict | None:
    """The order's customer for cost per job: the default group of its contact's billing
    account, once that group is in QuickBooks. A card customer isn't in QuickBooks: none.
    Orders don't pick a group yet (Phase 3), so the default group stands in."""
    from apps.billing.models import AccountGroup

    group = (
        AccountGroup.objects.filter(account__contact_id=lead.contact_id, is_default=True)
        .exclude(qbo_id="")
        .first()
    )
    if group is None or (group.qbo_realm_id and group.qbo_realm_id != realm):
        return None
    return {"value": group.qbo_id}


def bill_members(payable: AffiliatePayable) -> list[AffiliatePayable]:
    """The payables one QuickBooks Bill carries — the only place that decides it.

    One today: a Bill per approved payable (one trip), its link (`qbo_*`, the attachment) on
    that payable. The likely change is one Bill per *vendor invoice* — an affiliate sends one
    invoice number and file covering several trips — and then this returns that invoice's
    payables and the link moves to whatever record owns the invoice. Every payload, total and
    "is it in step" check below reads the members from here, never assumes one.
    """
    return [payable]


def _member_lines(payable: AffiliatePayable, account: dict, realm: str) -> list[dict]:
    """One trip's lines: base payout, then overtime — what the affiliate invoiced for it,
    split for cost reporting, each carrying the trip's customer for cost per job.

    They always add up to the trip's invoice amount (what was approved and accrued) and are
    never negative: an invoice under the agreed payout is one base line for the invoice.
    Anything over the payout is the second line — the affiliate's overtime, or the override,
    plus any difference approved with a note.
    """
    trip = payable.assignment.reservation
    invoice = Decimal(payable.invoice_amount or 0)
    base = min(Decimal(payable.assignment.payout or 0), invoice)
    extra = invoice - base
    detail = {"AccountRef": account}
    customer = _customer_ref(trip.lead, realm)
    if customer:
        detail |= {"CustomerRef": customer, "BillableStatus": "NotBillable"}
    lines = []
    for amount, label in ((base, "base payout"), (extra, "affiliate overtime")):
        if amount > ZERO:
            description = f"{trip.reference} {label}"
            if label != "base payout" and payable.variance and payable.approval_note:
                description += f" (approved: {payable.approval_note})"
            lines.append(
                {
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Amount": float(amount),
                    "Description": description,
                    "AccountBasedExpenseLineDetail": dict(detail),
                }
            )
    return lines


def bill_total(payable: AffiliatePayable) -> Decimal:
    return sum((Decimal(m.invoice_amount or 0) for m in bill_members(payable)), ZERO)


def bill_payload(payable: AffiliatePayable, realm: str) -> dict:
    """The whole Bill. Updates send it whole too (with Id and SyncToken): Intuit documents a
    full update for Bill, and every field here is the portal's, so nothing is lost."""
    account = expense_account_ref()
    if account is None:
        raise _Refused(_NO_EXPENSE_ACCOUNT, alert=False)
    members = bill_members(payable)
    number = payable.invoice_number
    trips = ", ".join(m.assignment.reservation.reference for m in members)
    orders = ", ".join(dict.fromkeys(m.assignment.reservation.lead.quote_no for m in members))
    note = f"{trips} · order {orders}"
    if number:
        note += f" · affiliate invoice {number}"
    first_trip = members[0].assignment.reservation
    body = {
        "VendorRef": {"value": payable.assignment.vendor.qbo_id},
        "TxnDate": _trip_date(
            payable.invoice_received_at or payable.approved_at, first_trip
        ).isoformat(),
        "PrivateNote": note,
        "Line": [line for m in members for line in _member_lines(m, account, realm)],
    }
    if number:
        body["DocNumber"] = number[:DOC_NUMBER_MAX]
    return body


def _cents(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _same_total(obj: dict, expected: Decimal) -> bool:
    """QuickBooks' TotalAmt is what we sent, to the cent. Missing counts as different."""
    return "TotalAmt" in obj and _cents(obj["TotalAmt"]) == _cents(expected)


def _bill_matches(obj: dict, body: dict) -> bool:
    if "DocNumber" in body and obj.get("DocNumber", body["DocNumber"]) != body["DocNumber"]:
        return False
    vendor = _ref_value(obj, "VendorRef")
    if vendor is not None and vendor != body["VendorRef"]["value"]:
        return False
    sent = sum((Decimal(str(line["Amount"])) for line in body["Line"]), ZERO)
    return _same_total(obj, sent)


def _payable(payable: AffiliatePayable) -> AffiliatePayable:
    return AffiliatePayable.objects.select_related(
        "assignment__vendor", "assignment__reservation__lead"
    ).get(pk=payable.pk)


def _is_bill(payable: AffiliatePayable) -> bool:
    """Approved or paid, for more than nothing — the same rule as `_billable()`."""
    return payable.status in _BILLED and Decimal(payable.invoice_amount or 0) > ZERO


def _push_bill(payable: AffiliatePayable) -> bool:
    """Create or update the payable's Bill, then attach the invoice file once. True when both
    are in step. Waits (False, no error) while its vendor isn't in QuickBooks."""
    payable = _payable(payable)
    if not _is_bill(payable):
        return False
    realm = customers.current_realm()
    vendor = payable.assignment.vendor
    if not vendor.qbo_id or (vendor.qbo_realm_id and vendor.qbo_realm_id != realm):
        return False
    link = _Link(payable, "bill", "Bill")
    if _relink_if_other_company(link, realm):
        # A new bill in this company: the file goes up to it afresh.
        AffiliatePayable.objects.filter(pk=payable.pk).update(
            qbo_attachment_id="", qbo_attachment_sync_state=SyncState.NOT_SYNCED
        )
    return _push(
        link,
        lambda: bill_payload(payable, realm),
        matches=_bill_matches,
        what="bill",
    )


# --- the invoice file (Attachable) ---------------------------------------------------------------

# Intuit community posts put the per-attachment limit around 20 MB (unverified); refuse a
# bigger file here with a sentence rather than send it and get a vague refusal back.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def upload_attachment(
    *, entity: str, entity_id: str, name: str, content: bytes, ctype: str, request_id: str
) -> str:
    """Attach a file to a QuickBooks transaction: Intuit's multipart `upload` endpoint, one
    `file_metadata_01` (the Attachable as JSON) and one `file_content_01` (the bytes). Returns
    the Attachable's id. A refusal of the file comes back inside a 200, per item.

    UNVERIFIED against the sandbox: the part names and JSON metadata follow Intuit's SDKs.
    """
    meta = {
        "AttachableRef": [{"EntityRef": {"type": entity, "value": entity_id}}],
        "FileName": name,
        "ContentType": ctype,
    }
    result = qbo.request(
        "POST",
        "upload",
        params={"requestid": request_id[:50]},
        files={
            "file_metadata_01": (None, json.dumps(meta), "application/json"),
            "file_content_01": (name, content, ctype),
        },
    )
    items = result.get("AttachableResponse") or [{}]
    item = items[0] if isinstance(items, list) else items
    if "Fault" in item:
        errors = item["Fault"].get("Error") or [{}]
        error = errors[0] if isinstance(errors, list) else errors
        reason = error.get("Detail") or error.get("Message") or "no reason given"
        raise _Refused(f"QuickBooks didn't accept the invoice file: {reason}", by_quickbooks=True)
    try:
        return str(item["Attachable"]["Id"])
    except (KeyError, TypeError) as exc:
        raise _Refused("QuickBooks didn't confirm the invoice file. Retry.") from exc


def _file_content(payable: AffiliatePayable) -> tuple[str, bytes, str]:
    name = os.path.basename(payable.invoice_file.name)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        size = payable.invoice_file.size
    except OSError as exc:
        raise _Refused("The invoice file couldn't be read here. Re-upload it, then retry.") from exc
    if size > MAX_ATTACHMENT_BYTES:
        raise _Refused(
            "The invoice file is over 20 MB, more than QuickBooks takes. Attach a smaller copy"
            " in QuickBooks by hand, or ask the affiliate for a smaller file."
        )
    try:
        with payable.invoice_file.open("rb") as handle:
            content = handle.read()
    except OSError as exc:
        raise _Refused("The invoice file couldn't be read here. Re-upload it, then retry.") from exc
    return name, content, ctype


def _push_attachment(payable: AffiliatePayable) -> bool:
    """Upload the payable's invoice file to its Bill, once. Its own state and error: a file
    QuickBooks refuses never holds up the bill or its payment. Waits (False, no error) for the
    bill to be in QuickBooks in this company."""
    payable = _payable(payable)
    if not _is_bill(payable) or not payable.invoice_file:
        return False
    realm = customers.current_realm()
    if not (payable.qbo_id and payable.qbo_realm_id == realm):
        return False
    link = _Link(payable, "attachment", "Attachable", prefix="qbo_attachment_")
    _relink_if_other_company(link, realm)
    if payable.qbo_attachment_id:
        return payable.qbo_attachment_sync_state == SyncState.SYNCED
    try:
        name, content, ctype = _file_content(payable)
        file_key = _digest({"f": name, "n": len(content)})
        attempt = payable.qbo_attachment_create_attempt
        attachment_id = upload_attachment(
            entity="Bill",
            entity_id=payable.qbo_id,
            name=name,
            content=content,
            ctype=ctype,
            # Keyed on the bill and the file: a replaced file never replays the old answer.
            request_id=f"apc-b{payable.pk}-att{payable.qbo_id}-{file_key}{attempt or ''}",
        )
    except _Refused as exc:
        if exc.by_quickbooks:
            customers.refused_by_quickbooks(payable, link.prefix)
        _fail(link, str(exc), alert=exc.alert)
        return False
    except qbo.QboAPIError as exc:
        logger.warning("QuickBooks refused the file for payable %s: %s", payable.pk, exc.body[:500])
        if _transient(exc):
            raise SyncPaused(str(exc)) from exc
        customers.refused_by_quickbooks(payable, link.prefix)
        _fail(link, _reason(exc, "invoice file", article="the"))
        return False
    link.update(
        id=attachment_id,
        realm_id=realm,
        synced_at=timezone.now(),
        sync_state=SyncState.SYNCED,
        sync_error="",
    )
    return True


# --- bill payments -----------------------------------------------------------------------------


def payment_payload(payable: AffiliatePayable) -> dict:
    """Check is the only pay type for money from a bank account (QuickBooks has Check and
    CreditCard); ACH, Zelle and other bank transfers go as Check with the method in the memo."""
    account = payment_account_ref()
    if account is None:
        raise _Refused(_NO_PAYMENT_ACCOUNT, alert=False)
    trip = payable.assignment.reservation
    amount = float(bill_total(payable))
    note = f"Paid by {payable.get_paid_method_display() or 'other'}"
    if payable.paid_reference:
        note += f" · {payable.paid_reference}"
    note += f" · {trip.reference}"
    body = {
        "VendorRef": {"value": payable.assignment.vendor.qbo_id},
        "PayType": "Check",
        "CheckPayment": {"BankAccountRef": account},
        "TotalAmt": amount,
        "TxnDate": _trip_date(payable.paid_at, trip).isoformat(),
        "PrivateNote": note,
        "Line": [{"Amount": amount, "LinkedTxn": [{"TxnId": payable.qbo_id, "TxnType": "Bill"}]}],
    }
    if payable.paid_reference:
        body["DocNumber"] = payable.paid_reference[:DOC_NUMBER_MAX]
    return body


def _payment_matches(obj: dict, body: dict) -> bool:
    vendor = _ref_value(obj, "VendorRef")
    if vendor is not None and vendor != body["VendorRef"]["value"]:
        return False
    return _same_total(obj, Decimal(str(body["TotalAmt"])))


def _bill_in_step(payable: AffiliatePayable, realm: str) -> bool:
    return (
        bool(payable.qbo_id)
        and payable.qbo_realm_id == realm
        and payable.qbo_sync_state == SyncState.SYNCED
    )


def _push_payment(payable: AffiliatePayable) -> bool:
    """Create or update the BillPayment for a paid payable. Waits (False, no error) until its
    Bill is in step in this company."""
    payable = _payable(payable)
    realm = customers.current_realm()
    if payable.status != AffiliatePayable.Status.PAID or not _bill_in_step(payable, realm):
        return False
    link = _Link(payable, "billpayment", "BillPayment", prefix="qbo_payment_")
    return _push(
        link,
        lambda: payment_payload(payable),
        matches=_payment_matches,
        what="bill payment",
    )


# --- the run -----------------------------------------------------------------------------------

_BILLED = (AffiliatePayable.Status.APPROVED, AffiliatePayable.Status.PAID)


def _stale(prefix: str, realm: str) -> Q:
    """Not in step: never pushed, changed, refused — or linked to another company."""
    stale = Q(**{f"{prefix}sync_state__in": (SyncState.NOT_SYNCED, SyncState.ERROR)})
    other_company = ~Q(**{f"{prefix}id": ""}) & ~Q(**{f"{prefix}realm_id": realm})
    return stale | other_company


def _billable():
    """Payables that are bills: approved or paid, for more than nothing."""
    return AffiliatePayable.objects.filter(status__in=_BILLED, invoice_amount__gt=0)


def pending_vendors(realm: str):
    """Vendors that need pushing: only those with a bill to carry, or already linked here."""
    from apps.vendors.models import Vendor

    has_bill = Exists(_billable().filter(assignment__vendor=OuterRef("pk")))
    linked_here = ~Q(qbo_id="") & Q(qbo_realm_id=realm)
    return (
        Vendor.objects.filter(_stale("qbo_", realm)).filter(has_bill | linked_here).order_by("pk")
    )


def pending_bills(realm: str):
    return (
        _billable()
        .filter(_stale("qbo_", realm))
        .select_related("assignment__vendor", "assignment__reservation__lead")
        .order_by("pk")
    )


def pending_payments(realm: str):
    return (
        _billable()
        .filter(status=AffiliatePayable.Status.PAID)
        .filter(_stale("qbo_payment_", realm))
        .select_related("assignment__vendor", "assignment__reservation__lead")
        .order_by("pk")
    )


def pending_attachments(realm: str):
    """Invoice files not yet on their bill, once the bill is in QuickBooks in this company."""
    return (
        _billable()
        .exclude(invoice_file="")
        .exclude(qbo_id="")
        .filter(qbo_realm_id=realm)
        .filter(_stale("qbo_attachment_", realm))
        .order_by("pk")
    )


def _block_all(rows, prefix: str, message: str) -> None:
    """Every pending record shares one missing choice: mark each (so the payable says why),
    alert once for the run — not once per bill."""
    from apps.notifications.models import Notification

    rows = list(rows)
    fresh = [
        r
        for r in rows
        if getattr(r, prefix + "sync_state") != SyncState.ERROR
        or getattr(r, prefix + "sync_error") != message
    ]
    if not fresh:
        return
    AffiliatePayable.objects.filter(pk__in=[r.pk for r in rows]).update(
        **{prefix + "sync_state": SyncState.ERROR, prefix + "sync_error": message}
    )
    Notification.notify(
        None,
        Notification.Kind.SYNC_FAILED,
        title="Affiliate bills can't go to QuickBooks yet",
        detail=message,
    )


def sync_pending() -> int:
    """Push up to SYNC_BATCH vendors, bills, bill payments and invoice files that aren't in
    step, in that order (each waits for the one it hangs off). Returns how many were pushed.
    Stops quietly on an outage, a rate limit or a lost connection — nothing is marked, the
    next run carries on."""
    if not qbo.is_connected():
        return 0
    realm = customers.current_realm()
    pushed = attempted = 0
    try:
        for vendor in pending_vendors(realm):
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push_vendor(vendor)
        bills = pending_bills(realm)
        if expense_account_ref() is None:
            _block_all(bills, "qbo_", _NO_EXPENSE_ACCOUNT)
        else:
            for payable in bills:
                if not _in_company(payable.assignment.vendor, realm):
                    continue  # waits for its vendor
                if attempted >= SYNC_BATCH:
                    return pushed
                attempted += 1
                pushed += push_bill(payable)
        payments = [p for p in pending_payments(realm) if _bill_in_step(p, realm)]
        if payments and payment_account_ref() is None:
            _block_all(payments, "qbo_payment_", _NO_PAYMENT_ACCOUNT)
        elif payments:
            for payable in payments:
                if attempted >= SYNC_BATCH:
                    return pushed
                attempted += 1
                pushed += push_payment(payable)
        # Files last: a slow or refused upload never holds up a bill or a payment.
        for payable in pending_attachments(realm):
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push_attachment(payable)
    except (SyncPaused, qbo.QboNotConnected) as exc:
        logger.warning("QuickBooks bill sync paused: %s", exc)
    return pushed


def push_everything_for(payable: AffiliatePayable) -> None:
    """A person's Retry on one payable: its vendor if needed, its bill, then its payment.
    Raises SyncPaused / QboNotConnected for the caller to word."""
    payable = _payable(payable)
    if not _is_bill(payable):
        return
    realm = customers.current_realm()
    vendor = payable.assignment.vendor
    if not _in_company(vendor, realm) or vendor.qbo_sync_state != SyncState.SYNCED:
        if not push_vendor(vendor):
            return
    payable.refresh_from_db()
    if payable.qbo_sync_state != SyncState.SYNCED or not _bill_in_step(payable, realm):
        if not push_bill(payable):
            return
    payable.refresh_from_db()
    if payable.status == AffiliatePayable.Status.PAID:
        push_payment(payable)
    push_attachment(payable)


def push_vendor(vendor) -> bool:
    """Create, link or update the vendor in QuickBooks, holding its lease. True when it is now
    in step; False (no call) while another push has it."""
    from apps.vendors.models import Vendor

    with _leased(Vendor, vendor.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        vendor.refresh_from_db()
        return _push_vendor(vendor)


def push_bill(payable: AffiliatePayable) -> bool:
    with _leased(AffiliatePayable, payable.pk, "qbo_push_started_at") as mine:
        return mine and _push_bill(payable)


def push_payment(payable: AffiliatePayable) -> bool:
    with _leased(AffiliatePayable, payable.pk, "qbo_payment_push_started_at") as mine:
        return mine and _push_payment(payable)


def push_attachment(payable: AffiliatePayable) -> bool:
    with _leased(AffiliatePayable, payable.pk, "qbo_attachment_push_started_at") as mine:
        return mine and _push_attachment(payable)


def run_hourly() -> int:
    """The `retry-qbo-sync` cron: customers first (APC-42: terms, accounts, groups), then the
    chart of accounts, then vendors, bills and payments. A failed accounts refresh doesn't stop
    the push: the cached choice still stands. Does nothing without a live connection."""
    if not qbo.is_connected():
        return 0
    pushed = customers.run_hourly()
    try:
        refresh_accounts()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        logger.warning("QuickBooks accounts refresh failed; pushing with the cached choice")
    return pushed + sync_pending()


def sync_state(payable: AffiliatePayable) -> dict | None:
    """What the payable pane shows: the bill's and the payment's state and sentence, and where
    Retry posts. None while QuickBooks isn't connected (no wall of red chips)."""
    from django.urls import reverse

    if not qbo.is_connected():
        return None
    return {
        "bill_state": payable.qbo_sync_state,
        "bill_error": payable.qbo_sync_error,
        "payment_state": payable.qbo_payment_sync_state,
        "payment_error": payable.qbo_payment_sync_error,
        "attachment_state": payable.qbo_attachment_sync_state if payable.invoice_file else "",
        "attachment_error": payable.qbo_attachment_sync_error if payable.invoice_file else "",
        "sync_url": reverse("payable_sync", args=[payable.pk]),
    }
