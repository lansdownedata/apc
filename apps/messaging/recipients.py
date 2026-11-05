"""Who gets a message (spec C section 4).

`resolve` turns a `MessageTemplate`'s recipient keys into people at send time, so a mobile
added later is used. `addresses` then picks one address per person per channel and drops
duplicates. Resolution never touches the network or writes.

How a caller tells the two empty cases apart: `resolve(...) == []` means nobody resolved
("No recipient"); a non-empty list whose `addresses(..., channel)` is empty means people
resolved but none has an address on that channel ("No mobile on file").
"""

from dataclasses import dataclass

from apps.core.phone import to_e164
from apps.leads.models import LeadContact

# Keys that name a customer-side person; on a farmed-in order they collapse to the operator.
_CUSTOMER_KEYS = frozenset({"passengers", "booking_contact", "billing_contact", "planner"})


@dataclass(frozen=True)
class Recipient:
    name: str
    label: str
    phone: str  # E.164 where parseable, else as typed; "" when none
    email: str
    contact_id: int | None


def _phone(raw: str | None) -> str:
    raw = (raw or "").strip()
    return to_e164(raw) or raw


def _person(contact, why: str) -> Recipient:
    return Recipient(
        name=contact.name,
        label=f"{contact.name} ({why})",
        phone=_phone(contact.phone),
        email=(contact.email or "").strip(),
        contact_id=contact.pk,
    )


def _passengers(lead, reservation) -> list[Recipient]:
    people = list(reservation.named_passengers.all()) if reservation is not None else []
    lead_passenger = next((p for p in people if p.is_lead), None)
    chosen = [lead_passenger] if lead_passenger else [p for p in people if p.mobile]
    if not chosen:
        return [_person(lead.contact, "booking contact")]
    return [
        Recipient(
            name=p.name,
            label=f"{p.name or p.mobile} ({'lead passenger' if p.is_lead else 'passenger'})",
            phone=_phone(p.mobile),
            email="",
            contact_id=None,
        )
        for p in chosen
    ]


def _planners(lead) -> list[Recipient]:
    found = []
    for link in lead.people.all():
        if link.role != LeadContact.Role.LEAD_PLANNER:
            continue
        account = getattr(link.contact, "planner_account", None)
        if account is None or account.revoked_at is not None:
            continue
        found.append(
            Recipient(
                name=link.contact.name,
                label=f"{link.contact.name} (planner)",
                phone="",
                email=account.email.strip(),
                contact_id=link.contact_id,
            )
        )
    return found


def _confirmed(reservation):
    if reservation is None:
        return None
    from apps.dispatch.selectors import confirmed_assignment

    return confirmed_assignment(reservation)


def _company(reservation) -> list[Recipient]:
    assignment = _confirmed(reservation)
    vendor = assignment.vendor if assignment is not None and assignment.vendor_id else None
    if vendor is None:
        return []
    return [
        Recipient(
            name=vendor.name,
            label=f"{vendor.name} (company)",
            phone=_phone(vendor.phone),
            email=(vendor.email or "").strip(),
            contact_id=None,
        )
    ]


def _driver(reservation) -> list[Recipient]:
    assignment = _confirmed(reservation)
    info = assignment.driver_info if assignment is not None else None
    if not info:
        return []
    return [
        Recipient(
            name=info.get("name", ""),
            label=f"{info.get('name', '')} (driver)",
            phone=_phone(info.get("cell")),
            email="",
            contact_id=None,
        )
    ]


def _role(lead, role: str) -> list[Recipient]:
    label = LeadContact.Role(role).label
    return [_person(link.contact, label) for link in lead.people.all() if link.role == role]


def _staff(row) -> list[Recipient]:
    return [
        Recipient(name=e, label="Staff", phone="", email=e.strip(), contact_id=None)
        for e in row.staff_emails or []
        if e and e.strip()
    ]


def resolve(row, lead, reservation=None) -> list[Recipient]:
    """The row's recipients for this order (and trip). Unknown keys are ignored.

    A farmed-in order (`lead.is_farm_in`) replaces every customer-side key with the sending
    operator, once. Vendor and driver keys are unaffected.
    """
    if row.audience == row.Audience.STAFF:
        return _staff(row)

    operator = lead.is_farm_in
    operator_added = False
    out: list[Recipient] = []
    for key in row.recipients or []:
        kind, _, arg = key.partition(":")
        if operator and (key in _CUSTOMER_KEYS or kind == "role"):
            if not operator_added:
                out.append(_person(lead.contact, "operator"))
                operator_added = True
            continue
        if key == "passengers":
            out += _passengers(lead, reservation)
        elif key == "booking_contact":
            out.append(_person(lead.contact, "booking contact"))
        elif key == "billing_contact":
            out.append(_person(lead.effective_billing_contact, "billing contact"))
        elif kind == "role" and arg in LeadContact.Role.values:
            out += _role(lead, arg)
        elif key == "company":
            out += _company(reservation)
        elif key == "driver":
            out += _driver(reservation)
        elif key == "planner":
            out += _planners(lead)
    return out


def addresses(recipients: list[Recipient], channel: str) -> list[tuple[Recipient, str]]:
    """One (recipient, address) per distinct address on `channel` ("sms" or "email").
    The first recipient to claim an address keeps it, so its label is the one shown."""
    seen: set[str] = set()
    out = []
    for r in recipients:
        address = r.phone if channel == "sms" else r.email.lower()
        if address and address not in seen:
            seen.add(address)
            out.append((r, address))
    return out
