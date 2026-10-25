"""Push billing accounts and groups to QuickBooks Online (APC-42) — outbound only.

* An account is a Customer; each group is a sub-customer (`ParentRef` + `Job: true`, plain
  group name) with
  `BillWithParent: false`, so invoices bill on the group itself (Moe's decision).
* Nothing here is called from a request that saves a record: saving marks it NOT_SYNCED, and
  the hourly `retry-qbo-sync` cron (or a person's "Retry sync") pushes it. QuickBooks is
  never on the critical path of the portal.
* Idempotent: Intuit's `requestid` (≤50 chars) is derived from the record and the change, so
  a retried create whose response was lost returns the original customer, not a duplicate.
* A group waits while its account has no QuickBooks id; it doesn't error on its own.
* An outage or rate limit stops the run and marks nothing — the next run picks it up. Only
  a definite refusal from QuickBooks marks a record ERROR (one alert per failure).
* A record is only marked SYNCED if nobody edited it while the push was in flight, and only
  when what QuickBooks returned matches what was sent.
* `qbo_realm_id` ties each link to a QuickBooks company: connect a different company and
  everything is pushed to it as new.
* A term QuickBooks doesn't have is an error, never silently dropped (the ticket: "do not
  silently change a customer's terms").
* Never call this inside an outer transaction — the client's token refresh is durable.

Verified API facts: docs/superpowers/specs/2026-09-19-billing-accounts/qbo-notes.md.
"""

from __future__ import annotations

import hashlib
import json
import logging

from django.db.models import Q
from django.utils import timezone

from apps.integrations import qbo

from .models import AccountGroup, BillingAccount, QboTerm, SyncState, Terms

logger = logging.getLogger(__name__)

DUPLICATE_NAME = "6240"
STALE_OBJECT = "5010"
# Records pushed per run: a big first sync spreads over a few hourly runs instead of one long
# request that a worker timeout would kill.
SYNC_BATCH = 25
_DUPLICATE_MESSAGE = (
    "QuickBooks already has a customer with this name. Rename it here or there, then retry."
)


class SyncPaused(Exception):
    """QuickBooks is unreachable or throttling us: stop, mark nothing, try again next run."""


class _Refused(Exception):
    """A definite problem with this record (shown to staff); not a reason to stop the run."""


def _kind(record) -> str:
    return "group" if isinstance(record, AccountGroup) else "account"


def current_realm() -> str:
    from apps.integrations.models import QboCredential

    return QboCredential.load().realm_id


def display_name(record) -> str:
    """The name QuickBooks shows. A group is its plain name ("Accounts Payable"): QuickBooks
    lets sub-customers of different accounts share one (Moe, 2026-10-25), and it shows each
    under its account."""
    return record.name


def _who(record) -> str:
    """Which record, for staff alerts: a group's name alone doesn't say whose it is."""
    if isinstance(record, AccountGroup):
        return f"{record.account.name} - {record.name}"
    return record.name


def _label(term: str) -> str:
    return Terms(term).label if term in Terms.values else term


def term_ref(term: str) -> dict | None:
    """Our term matched to a cached QuickBooks term by name, or None — never a guess."""
    if not term:
        return None
    match = QboTerm.objects.filter(active=True, name__iexact=_label(term)).first()
    return {"value": match.qbo_id} if match else None


def unmatched_terms() -> list[str]:
    """Terms our accounts and groups actually use that QuickBooks has no term for."""
    names = {n.lower() for n in QboTerm.objects.filter(active=True).values_list("name", flat=True)}
    used = set(BillingAccount.objects.values_list("terms", flat=True))
    used |= set(AccountGroup.objects.exclude(terms="").values_list("terms", flat=True))
    return [label for value, label in Terms.choices if value in used and label.lower() not in names]


def _terms_of(record) -> str:
    return record.effective_terms if isinstance(record, AccountGroup) else record.terms


def payload(record) -> dict:
    body = {"DisplayName": display_name(record), "BillWithParent": False}
    if isinstance(record, AccountGroup):
        body["ParentRef"] = {"value": record.account.qbo_id}
        body["Job"] = True
        if record.invoice_email or record.qbo_id:
            # On an update an empty Address clears the old one; leaving the key out of a
            # sparse update would keep QuickBooks sending invoices to a removed address.
            body["PrimaryEmailAddr"] = {"Address": record.invoice_email}
    terms = _terms_of(record)
    if terms:
        ref = term_ref(terms)
        if ref is None:
            raise _Refused(
                f"QuickBooks has no “{_label(terms)}” term. Add it in QuickBooks, refresh the terms"
                " in Settings, then retry."
            )
        body["SalesTermRef"] = ref
    return body


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]


def _request_id(record, body: dict) -> str:
    """Deterministic per record and change: the same retry gets the same id, so QuickBooks
    returns the original result. A create keeps one id across outages (a lost response then
    replays the customer that was made) but takes a new one after QuickBooks definitely
    refused it, so the corrected record isn't answered with the old refusal."""
    kind = f"{_kind(record)[0]}{record.pk}"
    if not record.qbo_id:
        # qbo_sync_error is only ever set by a definite refusal (outages mark nothing) and
        # survives a local edit, unlike the state.
        if record.qbo_sync_error:
            return f"apc-{_digest(body)}-{kind}-c"[:50]
        return f"apc-{kind}-create"
    return f"apc-{_digest(body)}-{kind}-u{record.qbo_sync_token}"[:50]


def _errors(exc: qbo.QboAPIError) -> list[dict]:
    try:
        errors = json.loads(exc.body).get("Fault", {}).get("Error", [])
    except (ValueError, AttributeError, TypeError):
        return []
    if isinstance(errors, dict):
        errors = [errors]
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _fault_code(exc: qbo.QboAPIError) -> str:
    errors = _errors(exc)
    return str(errors[0].get("code", "")) if errors else ""


def _transient(exc: qbo.QboAPIError) -> bool:
    return isinstance(exc, qbo.QboRateLimited) or exc.status == 0 or exc.status >= 500


def _refusal_message(exc: qbo.QboAPIError) -> str:
    if _fault_code(exc) == DUPLICATE_NAME:
        return _DUPLICATE_MESSAGE
    errors = _errors(exc)
    reason = (errors[0].get("Detail") or errors[0].get("Message")) if errors else ""
    if reason:
        return f"QuickBooks didn't accept this record: {reason}"
    return f"QuickBooks didn't accept this record ({exc.status}). Check it, then retry."


def _fail(record, message: str) -> None:
    from apps.notifications.models import Notification

    first_failure = record.qbo_sync_state != SyncState.ERROR
    record.qbo_sync_state = SyncState.ERROR
    record.qbo_sync_error = message[:255]
    type(record).objects.filter(pk=record.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error=record.qbo_sync_error
    )
    if first_failure:  # one alert per failure, not one per hourly retry
        Notification.notify(
            None,
            Notification.Kind.SYNC_FAILED,
            title=f"QuickBooks sync failed: {_who(record)}",
            detail=record.qbo_sync_error,
        )


def _send(record, body: dict) -> dict:
    if record.qbo_id:
        body = {**body, "Id": record.qbo_id, "SyncToken": record.qbo_sync_token, "sparse": True}
    return qbo.request(
        "POST", "customer", params={"requestid": _request_id(record, body)}, json=body
    )


def _matches(customer: dict, body: dict) -> bool:
    """Did QuickBooks return what we sent? A replayed create answers with an older record."""
    if "DisplayName" in customer and customer["DisplayName"] != body["DisplayName"]:
        return False
    parent = customer.get("ParentRef", {}).get("value") if customer.get("ParentRef") else None
    return not ("ParentRef" in body and parent is not None and parent != body["ParentRef"]["value"])


def push(record) -> bool:
    """Create or update `record` in QuickBooks. True when it is now in step.

    Raises SyncPaused on an outage or rate limit (nothing is marked; retry later) and
    QboNotConnected when there's no live connection — both stop a run.
    """
    if isinstance(record, AccountGroup):
        # Read the account fresh: a group reached through `account.groups` carries the
        # caller's in-memory account, which may predate its first push.
        record.account.refresh_from_db(fields=["name", "qbo_id", "qbo_realm_id"])
        if not record.account.qbo_id:
            return False  # waits for its account; not an error of its own
    realm = current_realm()
    if record.qbo_id and record.qbo_realm_id and record.qbo_realm_id != realm:
        record.qbo_id = record.qbo_sync_token = ""  # linked to another company: push as new
    snapshot = record.updated_at
    try:
        body = payload(record)
        try:
            result = _send(record, body)
        except qbo.QboAPIError as exc:
            if not (record.qbo_id and _fault_code(exc) == STALE_OBJECT):
                raise
            # Edited in QuickBooks since our last push: take its current SyncToken, retry once.
            current = qbo.request("GET", f"customer/{record.qbo_id}")["Customer"]
            record.qbo_sync_token = str(current["SyncToken"])
            result = _send(record, body)
    except _Refused as exc:
        _fail(record, str(exc))
        return False
    except qbo.QboAPIError as exc:
        logger.warning("QuickBooks refused %s %s: %s", _kind(record), record.pk, exc.body[:500])
        if _transient(exc):
            raise SyncPaused(str(exc)) from exc
        _fail(record, _refusal_message(exc))
        return False
    customer = result["Customer"]
    model = type(record)
    model.objects.filter(pk=record.pk).update(
        qbo_id=str(customer["Id"]),
        qbo_sync_token=str(customer["SyncToken"]),
        qbo_realm_id=realm,
        qbo_synced_at=timezone.now(),
    )
    # SYNCED only if nobody edited the record while the push was in flight (the edit bumped
    # updated_at and set NOT_SYNCED) and QuickBooks returned what we sent.
    in_step = _matches(customer, body)
    if in_step:
        model.objects.filter(pk=record.pk, updated_at=snapshot).update(
            qbo_sync_state=SyncState.SYNCED, qbo_sync_error=""
        )
    else:
        model.objects.filter(pk=record.pk).update(qbo_sync_state=SyncState.NOT_SYNCED)
    record.refresh_from_db()
    return record.qbo_sync_state == SyncState.SYNCED


def _pending(model, realm: str):
    """Not in step: never pushed, changed, refused — or linked to another company."""
    stale = Q(qbo_sync_state__in=(SyncState.NOT_SYNCED, SyncState.ERROR))
    other_company = ~Q(qbo_id="") & ~Q(qbo_realm_id=realm)
    return model.objects.filter(stale | other_company)


def sync_pending() -> int:
    """Push up to SYNC_BATCH accounts, then groups, that aren't in step. The cron's job.
    Returns how many were pushed. Stops quietly on an outage, a rate limit, or a lost
    connection — nothing is marked, the next run carries on."""
    if not qbo.is_connected():
        return 0
    realm = current_realm()
    pushed = attempted = 0
    try:
        for account in _pending(BillingAccount, realm).order_by("pk"):
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push(account)
        groups = (
            _pending(AccountGroup, realm).select_related("account").order_by("account_id", "pk")
        )
        for group in groups:
            if not group.account.qbo_id or (
                group.account.qbo_realm_id and group.account.qbo_realm_id != realm
            ):
                continue  # waits for its account
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push(group)
    except (SyncPaused, qbo.QboNotConnected) as exc:
        logger.warning("QuickBooks sync paused: %s", exc)
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


def run_hourly() -> int:
    """The `retry-qbo-sync` cron: refresh the terms cache, then push what's pending.
    Does nothing without a live connection. A failed terms refresh doesn't stop the push."""
    if not qbo.is_connected():
        return 0
    try:
        refresh_terms()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        logger.warning("QuickBooks terms refresh failed; pushing with the cached terms")
    return sync_pending()
