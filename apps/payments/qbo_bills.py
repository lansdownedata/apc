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

import logging

from apps.billing import qbo_sync as customers
from apps.billing.models import QboAccount, QboAccountMapping
from apps.integrations import qbo

logger = logging.getLogger(__name__)


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
