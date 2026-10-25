"""Push billing accounts and groups to QuickBooks Online (APC-42) — outbound only.

* An account is a Customer; each group is a sub-customer (`ParentRef` + `Job: true`) with
  `BillWithParent: false`, so invoices bill on the group itself (Moe's decision).
* Nothing here is called from a request that saves a record: saving marks it NOT_SYNCED, and
  the hourly `retry-qbo-sync` cron (or a person's "Retry sync") pushes it. QuickBooks is
  never on the critical path of the portal.
* Idempotent: Intuit's `requestid` (≤50 chars) is derived from the record and the change, so
  a retried create whose response was lost returns the original customer, not a duplicate.
* A group waits while its account has no QuickBooks id; it doesn't error on its own.
* Never call this inside an outer transaction — the client's token refresh is durable.

Verified API facts: docs/superpowers/specs/2026-09-19-billing-accounts/qbo-notes.md.
"""

from __future__ import annotations

import hashlib
import json
import logging

from django.utils import timezone

from apps.integrations import qbo

from .models import AccountGroup, BillingAccount, QboTerm, SyncState, Terms

logger = logging.getLogger(__name__)

DUPLICATE_NAME = "6240"
STALE_OBJECT = "5010"
_DUPLICATE_MESSAGE = (
    "QuickBooks already has a customer with this name. Rename it here or there, then retry."
)


def _kind(record) -> str:
    return "group" if isinstance(record, AccountGroup) else "account"


def display_name(record) -> str:
    """QuickBooks needs DisplayName unique across every customer, vendor and employee, so a
    group carries its account's name in front ("Ridgeline Partners - Accounts Payable")."""
    if isinstance(record, AccountGroup):
        return f"{record.account.name} - {record.name}"
    return record.name


def term_ref(term: str) -> dict | None:
    """Our term matched to a cached QuickBooks term by name, or None — never a guess."""
    if not term:
        return None
    label = Terms(term).label if term in Terms.values else term
    match = QboTerm.objects.filter(active=True, name__iexact=label).first()
    return {"value": match.qbo_id} if match else None


def unmatched_terms() -> list[str]:
    """Our terms with no active QuickBooks term of the same name (shown on Settings)."""
    names = {n.lower() for n in QboTerm.objects.filter(active=True).values_list("name", flat=True)}
    return [label for _value, label in Terms.choices if label.lower() not in names]


def payload(record) -> dict:
    body = {"DisplayName": display_name(record), "BillWithParent": False}
    if isinstance(record, AccountGroup):
        body["ParentRef"] = {"value": record.account.qbo_id}
        body["Job"] = True
        if record.invoice_email:
            body["PrimaryEmailAddr"] = {"Address": record.invoice_email}
        terms = record.effective_terms
    else:
        terms = record.terms
    if ref := term_ref(terms):
        body["SalesTermRef"] = ref
    return body


def _request_id(record, body: dict) -> str:
    """Deterministic per record and change: the same retry gets the same id (QuickBooks then
    returns the original result); a different change gets a new one."""
    if not record.qbo_id:
        return f"apc-{_kind(record)}-{record.pk}-create"
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    return f"apc-{_kind(record)}-{record.pk}-u{record.qbo_sync_token}-{digest}"[:50]


def _fault_code(exc: qbo.QboAPIError) -> str:
    try:
        errors = json.loads(exc.body).get("Fault", {}).get("Error", [])
        return str(errors[0].get("code", "")) if errors else ""
    except (ValueError, AttributeError, IndexError):
        return ""


def _message(exc: qbo.QboAPIError) -> str:
    if _fault_code(exc) == DUPLICATE_NAME:
        return _DUPLICATE_MESSAGE
    if exc.status == 0 or exc.status >= 500 or exc.status == 429:
        return "QuickBooks couldn't be reached. It will try again within the hour."
    return f"QuickBooks didn't accept this record ({exc.status}). Check it, then retry."


def _fail(record, exc: qbo.QboAPIError) -> None:
    from apps.notifications.models import Notification

    first_failure = record.qbo_sync_state != SyncState.ERROR
    record.qbo_sync_state = SyncState.ERROR
    record.qbo_sync_error = _message(exc)[:255]
    record.save(update_fields=["qbo_sync_state", "qbo_sync_error", "updated_at"])
    if first_failure:  # one alert per failure, not one per hourly retry
        Notification.notify(
            None,
            Notification.Kind.SYNC_FAILED,
            title=f"QuickBooks sync failed: {display_name(record)}",
            detail=record.qbo_sync_error,
        )


def _send(record, body: dict) -> dict:
    if record.qbo_id:
        body = {**body, "Id": record.qbo_id, "SyncToken": record.qbo_sync_token, "sparse": True}
    return qbo.request(
        "POST", "customer", params={"requestid": _request_id(record, body)}, json=body
    )


def push(record) -> bool:
    """Create or update `record` in QuickBooks. True when it is now in step. Raises
    QboNotConnected (so a run stops) when there's no live connection."""
    if isinstance(record, AccountGroup) and not record.account.qbo_id:
        return False  # waits for its account; not an error of its own
    body = payload(record)
    try:
        try:
            result = _send(record, body)
        except qbo.QboAPIError as exc:
            if not (record.qbo_id and _fault_code(exc) == STALE_OBJECT):
                raise
            # Edited in QuickBooks since our last push: take its current SyncToken, retry once.
            current = qbo.request("GET", f"customer/{record.qbo_id}")["Customer"]
            record.qbo_sync_token = str(current["SyncToken"])
            result = _send(record, body)
    except qbo.QboAPIError as exc:
        _fail(record, exc)
        return False
    customer = result["Customer"]
    record.qbo_id = str(customer["Id"])
    record.qbo_sync_token = str(customer["SyncToken"])
    record.qbo_sync_state = SyncState.SYNCED
    record.qbo_sync_error = ""
    record.qbo_synced_at = timezone.now()
    record.save(
        update_fields=[
            "qbo_id",
            "qbo_sync_token",
            "qbo_sync_state",
            "qbo_sync_error",
            "qbo_synced_at",
            "updated_at",
        ]
    )
    return True


def sync_pending() -> int:
    """Push every account, then every group, that isn't in step. The cron's job. Returns how
    many were pushed. Stops quietly if the connection is missing or breaks mid-run."""
    if not qbo.is_connected():
        return 0
    pending = (SyncState.NOT_SYNCED, SyncState.ERROR)
    pushed = 0
    try:
        for account in BillingAccount.objects.filter(qbo_sync_state__in=pending).order_by("pk"):
            pushed += push(account)
        groups = AccountGroup.objects.filter(qbo_sync_state__in=pending).select_related("account")
        for group in groups.order_by("account_id", "pk"):
            pushed += push(group)
    except qbo.QboNotConnected:
        logger.warning("QuickBooks sync stopped: the connection isn't usable")
    return pushed


def refresh_terms() -> int:
    """Cache the company's payment terms; a term no longer in QuickBooks is retired, not
    deleted. Returns how many active terms QuickBooks has."""
    rows = qbo.request("GET", "query", params={"query": "select * from Term"})
    terms = rows.get("QueryResponse", {}).get("Term", [])
    seen = []
    for term in terms:
        QboTerm.objects.update_or_create(
            qbo_id=str(term["Id"]),
            defaults={"name": term.get("Name", "")[:100], "active": bool(term.get("Active", True))},
        )
        seen.append(str(term["Id"]))
    QboTerm.objects.exclude(qbo_id__in=seen).update(active=False)
    return QboTerm.objects.filter(active=True).count()
