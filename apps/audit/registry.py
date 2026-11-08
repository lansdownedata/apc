"""The one list of audited models (APC-140, spec §3.1 / §4.2).

Each `ModelSpec` names a model, the fields its trail leaves out, field labels, m2m fields,
the parent columns its entries are filed under (trip, order, profile) and a cheap label for
`object_repr`. `AuditConfig.ready()` registers every spec with auditlog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from auditlog.models import DEFAULT_OBJECT_REPR
from auditlog.registry import auditlog
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured, ObjectDoesNotExist
from django.db.models import Model
from django.utils.text import capfirst

from apps.audit.display import money

# Every registered model is a TimeStampedModel. auditlog's own setting for this
# (AUDITLOG_EXCLUDE_TRACKING_FIELDS) refuses to load without AUDITLOG_INCLUDE_ALL_MODELS,
# so the two timestamps are added here instead.
GLOBAL_EXCLUDE = ("created_at", "updated_at")

PARENT_KEYS = frozenset({"trip_id", "order_id", "profile"})

# Field labels shared by every model (spec §3.1); a spec's own `labels` override them. Any
# field not named here reads as its verbose_name, capitalised (`field_label`).
LABELS: dict[str, str] = {
    "hours": "Override hours",
    "min_hours": "Minimum hours",
    "affiliate_cost": "Vendor flat pay",
    "cost_ratio_pct": "Vendor factor",
    "pickup_timezone": "Time zone",
    "trip_status": "Status",
    "assigned_agent": "Agent",
    "customer_confirmed_at": "Customer confirmed",
    "accepted_terms_at": "Terms accepted",
    "fees_acknowledged_at": "Fees acknowledged",
    "affiliate_confirmed_at": "Affiliate confirmed",
    "expected_amount": "Expected cost",
    "invoice_amount": "Vendor invoice amount",
}


def _none(instance: Model) -> dict:
    return {}


@dataclass(frozen=True)
class ModelSpec:
    model: str
    exclude: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    m2m: frozenset[str] = frozenset()
    parents: Callable[[Model], dict] = _none
    label: Callable[[Model], str] = str
    sentences: dict[str, str] = field(default_factory=dict)
    # The row is mirrored in QuickBooks: its `qbo_*` sync bookkeeping is left out too
    # (`qbo_fields`, read from the model so the list can't drift).
    qbo: bool = False


def qbo_fields(model: type[Model]) -> tuple[str, ...]:
    """The model's QuickBooks sync columns: every one whose name starts with `qbo_`."""
    return tuple(f.name for f in model._meta.concrete_fields if f.name.startswith("qbo_"))


def _through(instance: Model, path: str) -> Any:
    """`instance.<fk>.<rest>` without loading the row: the related object when it is
    already cached, otherwise one `values_list` query. None when the FK is unset or the
    row is gone (a cascade delete logging its children), so the caller never raises."""
    head, _, rest = path.partition("__")
    if not rest:
        return getattr(instance, head, None)
    fk = instance._meta.get_field(head)
    if fk.is_cached(instance):
        related = fk.get_cached_value(instance)
        return None if related is None else _through(related, rest)
    pk = getattr(instance, fk.attname)
    if pk is None:
        return None
    return fk.related_model._base_manager.filter(pk=pk).values_list(rest, flat=True).first()


def _profile(kind: str, pk: int | None) -> str | None:
    return None if pk is None else f"{kind}:{pk}"


def _reservation_parents(reservation: Model) -> dict:
    return {"trip_id": reservation.pk, "order_id": reservation.lead_id}


def _reservation_label(reservation: Model) -> str:
    return reservation.reference


def _trip_child_parents(row: Model) -> dict:
    """TripExtra / Assignment: filed on their trip and its order."""
    return {"trip_id": row.reservation_id, "order_id": _through(row, "reservation__lead_id")}


def _order_child_parents(row: Model) -> dict:
    return {"order_id": row.lead_id}


def _contact_child_parents(row: Model) -> dict:
    return {"profile": _profile("contact", row.contact_id)}


def _vendor_child_parents(row: Model) -> dict:
    return {"profile": _profile("vendor", row.vendor_id)}


def _extra_label(extra: Model) -> str:
    return f"{extra.label or extra.get_kind_display()} {money(extra.amount)}"


def _assignment_label(assignment: Model) -> str:
    return "In-house assignment" if assignment.driver_id else "Vendor assignment"


def _lead_contact_label(row: Model) -> str:
    """The role, and the person when their row is already loaded (no query otherwise)."""
    role = str(row.get_role_display())
    contact_field = row._meta.get_field("contact")
    contact = contact_field.get_cached_value(row) if contact_field.is_cached(row) else None
    return f"{role}: {contact.name}" if contact is not None else role


def _payable_parents(payable: Model) -> dict:
    """Trip, order and vendor through the assignment: one query when it is not loaded."""
    field = payable._meta.get_field("assignment")
    if field.is_cached(payable):
        assignment = field.get_cached_value(payable)
        if assignment is None:
            return {}
        reservation_id, vendor_id = assignment.reservation_id, assignment.vendor_id
        lead_id = _through(assignment, "reservation__lead_id")
    elif payable.assignment_id is None:
        return {}
    else:
        row = (
            field.related_model._base_manager.filter(pk=payable.assignment_id)
            .values_list("reservation_id", "vendor_id", "reservation__lead_id")
            .first()
        )
        if row is None:
            return {}
        reservation_id, vendor_id, lead_id = row
    return {
        "trip_id": reservation_id,
        "order_id": lead_id,
        "profile": _profile("vendor", vendor_id),
    }


def _invoice_contact(invoice: Model) -> str | None:
    return _profile("contact", _through(invoice, "group__account__contact_id"))


def _invoice_line_parents(line: Model) -> dict:
    return {
        "trip_id": line.reservation_id,
        "order_id": _through(line, "reservation__lead_id"),
        "profile": _profile("contact", _through(line, "invoice__group__account__contact_id")),
    }


SPECS: list[ModelSpec] = [
    ModelSpec(
        model="reservations.Reservation",
        exclude=(
            "extras_total",
            "sort_order",
            "group_key",
            "source_leg_id",
            "la_reservation_id",
            "revenue_status",
            "recognized_at",
            "recognized_amount",
        ),
        parents=_reservation_parents,
        label=_reservation_label,
        sentences={"customer_confirmed_at": "Confirmed the trip"},
    ),
    ModelSpec(
        model="reservations.TripExtra",
        exclude=("created_by",),
        parents=_trip_child_parents,
        label=_extra_label,
    ),
    ModelSpec(
        model="dispatch.Assignment",
        exclude=(
            "gnet_transaction_id",
            "offered_at",
            "insurance_override_by",
            "insurance_override_at",
        ),
        parents=_trip_child_parents,
        label=_assignment_label,
        sentences={"affiliate_confirmed_at": "Confirmed the trip"},
    ),
    ModelSpec(
        model="leads.Lead",
        exclude=(
            "has_alert",
            "quote_viewed_at",
            "quote_view_count",
            "quote_last_viewed_at",
            "intake_payload",
            "fees_acknowledged_ip",
        ),
        parents=lambda lead: {"order_id": lead.pk},
        label=lambda lead: lead.quote_no,
        sentences={
            "accepted_terms_at": "Accepted the terms (version {accepted_terms_version})",
            "fees_acknowledged_at": "Acknowledged the cleaning fee and overtime terms",
        },
    ),
    ModelSpec(
        model="leads.LeadContact",
        parents=_order_child_parents,
        label=_lead_contact_label,
    ),
    ModelSpec(
        model="payments.PaymentPlan",
        exclude=("processor", "stripe_customer_id", "stripe_payment_method_id"),
        parents=_order_child_parents,
        label=lambda plan: "Payment plan",
    ),
    ModelSpec(
        model="contacts.Contact",
        exclude=("la_account_id", "podium_contact_uid", "stripe_customer_id"),
        parents=lambda contact: {"profile": _profile("contact", contact.pk)},
        label=lambda contact: contact.name,
    ),
    ModelSpec(
        model="contacts.ContactPhone",
        parents=_contact_child_parents,
        label=lambda phone: f"{phone.get_label_display()} {phone.number}",
    ),
    ModelSpec(
        model="contacts.Company",
        parents=lambda company: {"profile": _profile("company", company.pk)},
        label=lambda company: company.name,
    ),
    ModelSpec(
        model="billing.BillingAccount",
        qbo=True,
        parents=_contact_child_parents,
        label=lambda account: account.name,
    ),
    ModelSpec(
        model="billing.AccountGroup",
        qbo=True,
        parents=lambda group: {
            "profile": _profile("contact", _through(group, "account__contact_id"))
        },
        label=lambda group: group.name,
    ),
    ModelSpec(
        model="vendors.Vendor",
        exclude=("qbo_push_started_at",),
        qbo=True,
        m2m=frozenset({"vehicle_types"}),
        parents=lambda vendor: {"profile": _profile("vendor", vendor.pk)},
        label=lambda vendor: vendor.name,
    ),
    ModelSpec(
        model="vendors.VendorDriver",
        parents=_vendor_child_parents,
        label=lambda driver: driver.name,
    ),
    ModelSpec(
        model="vendors.VendorInsurance",
        parents=_vendor_child_parents,
        label=lambda policy: f"Insurance {policy.insurer}".strip(),
    ),
    ModelSpec(
        model="vendors.VendorDocument",
        exclude=("uploaded_by",),
        parents=_vendor_child_parents,
        label=lambda document: document.label,
    ),
    ModelSpec(
        model="payments.Charge",
        exclude=(
            "plan",
            "stripe_client_secret",
            "stripe_payment_intent_id",
            "stripe_refund_id",
            "idempotency_key",
            "attempt_no",
            "attempted_at",
            "authorized_at",
            "capture_expires_at",
            "captured_at",
            "overtime_amount",
            "overtime_gratuity",
            "receipt_due_at",
            "receipt_sent_at",
            "receipt_message_id",
            "receipt_lines",
            "receipt_parent",
            "receipt_attempted_at",
            "receipt_skipped_at",
        ),
        qbo=True,
        parents=lambda charge: {
            "trip_id": charge.reservation_id,
            "order_id": _through(charge, "plan__lead_id"),
        },
        label=lambda charge: f"{charge.get_kind_display()} {money(charge.amount)}",
    ),
    ModelSpec(
        model="payments.AffiliatePayable",
        exclude=("approved_by", "approved_at", "paid_at", "invoice_received_at"),
        qbo=True,
        parents=_payable_parents,
        label=lambda payable: "Vendor bill",
    ),
    ModelSpec(
        model="billing.Invoice",
        exclude=(
            "number",
            "created_by",
            "voided_by",
            "voided_at",
            "edited_by",
            "edited_at",
            "edit_note",
        ),
        qbo=True,
        parents=lambda invoice: {"profile": _invoice_contact(invoice)},
        label=lambda invoice: invoice.label,
    ),
    ModelSpec(
        model="billing.InvoiceLine",
        parents=_invoice_line_parents,
        label=lambda line: f"Invoice line {money(line.amount)}",
    ),
    ModelSpec(
        model="planners.PlannerRequest",
        exclude=("message", "task"),
        parents=_order_child_parents,
        label=lambda request: f"Planner request: {request.about}",
        sentences={"create": 'Planner request: "{about}"'},
    ),
]

_BY_LABEL = {spec.model.lower(): spec for spec in SPECS}


def spec_for(model_or_instance: type[Model] | Model) -> ModelSpec | None:
    return _BY_LABEL.get(model_or_instance._meta.label_lower)


def model_of(spec: ModelSpec) -> type[Model]:
    return apps.get_model(spec.model)


def effective_exclude(model_or_instance: type[Model] | Model) -> tuple[str, ...]:
    """Every field the trail leaves out for this model: its spec's, its QuickBooks sync
    columns when it is mirrored there, and the timestamps."""
    spec = spec_for(model_or_instance)
    if spec is None:
        return GLOBAL_EXCLUDE
    qbo = qbo_fields(model_or_instance._meta.model) if spec.qbo else ()
    return tuple(dict.fromkeys(spec.exclude + qbo + GLOBAL_EXCLUDE))


def field_label(model: type[Model], name: str) -> str:
    """How a field reads in the trail: the spec's own label, the shared map, else its
    verbose_name capitalised (or the name itself for a key that is not a field)."""
    spec = spec_for(model)
    if spec and name in spec.labels:
        return spec.labels[name]
    if name in LABELS:
        return LABELS[name]
    try:
        return str(capfirst(model._meta.get_field(name).verbose_name))
    except FieldDoesNotExist:
        return name


def parents(instance: Model) -> dict:
    """The parent columns an entry for `instance` is filed under; unset ones are left out."""
    spec = spec_for(instance)
    if spec is None:
        return {}
    return {
        key: value
        for key, value in spec.parents(instance).items()
        if key in PARENT_KEYS and value is not None
    }


def label(instance: Model) -> str:
    spec = spec_for(instance)
    try:
        return (spec.label if spec else str)(instance)
    except ObjectDoesNotExist:
        return DEFAULT_OBJECT_REPR


def _own_columns(model: type[Model]) -> list[str]:
    """The model's own columns. auditlog's create/delete diff otherwise walks reverse
    relations too, which costs a query per reverse one-to-one and caches "none" on the
    instance, so a related row created later (a trip's review) stays invisible on it."""
    return [f.name for f in model._meta.concrete_fields]


def _check(spec: ModelSpec, model: type[Model]) -> None:
    """A misspelt exclude would silently track the field it meant to leave out."""
    columns = set(_own_columns(model))
    unknown = set(effective_exclude(model)) - columns
    m2m = {f.name for f in model._meta.many_to_many}
    unknown |= set(spec.m2m) - m2m
    if unknown:
        raise ImproperlyConfigured(f"Audit spec {spec.model} names no such field: {unknown}")


def register_all() -> None:
    for spec in SPECS:
        model = model_of(spec)
        _check(spec, model)
        named = (LABELS.keys() | spec.labels.keys()) & (set(_own_columns(model)) | set(spec.m2m))
        auditlog.register(
            model,
            include_fields=_own_columns(model),
            exclude_fields=list(effective_exclude(model)),
            mapping_fields={name: field_label(model, name) for name in sorted(named)},
            m2m_fields=set(spec.m2m),
        )
