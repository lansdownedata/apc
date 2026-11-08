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

import re

from django.db.models import Exists, OuterRef, Q

from apps.billing.models import AccountGroup, BillingAccount, CustomerLink
from apps.billing.qbo_sync import DUPLICATE_NAME, _fault_code, current_realm
from apps.integrations import qbo
from apps.payments.qbo_bills import _leased, _Link, _push, _quoted, _Refused, _send, _stale

# QuickBooks' limit for a Customer's DisplayName.
DISPLAY_NAME_MAX = 100

# --- customers -----------------------------------------------------------------------------------


def _display_name(contact) -> str:
    return contact.name.strip()[:DISPLAY_NAME_MAX]


def _linked_here(link: CustomerLink | None, realm: str) -> bool:
    return bool(link and link.qbo_id and link.qbo_realm_id == realm)


def _link_of(contact) -> CustomerLink | None:
    return CustomerLink.objects.filter(contact=contact).first()


def _account_customer(contact, realm: str) -> BillingAccount | None:
    """The contact's billing account, when its Customer is in this company."""
    return contact.billing_accounts.exclude(qbo_id="").filter(qbo_realm_id=realm).first()


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


# --- the push ------------------------------------------------------------------------------------


# --- hourly --------------------------------------------------------------------------------------
