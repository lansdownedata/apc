"""The one list of audited models (APC-140, spec §3.1 / §4.2).

Each `ModelSpec` names a model, the fields its trail leaves out, field labels, m2m fields,
the parent columns its entries are filed under (trip, order, profile) and a cheap label for
`object_repr`. `AuditConfig.ready()` registers every spec with auditlog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from auditlog.models import DEFAULT_OBJECT_REPR
from auditlog.registry import auditlog
from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model

# Every registered model is a TimeStampedModel. auditlog's own setting for this
# (AUDITLOG_EXCLUDE_TRACKING_FIELDS) refuses to load without AUDITLOG_INCLUDE_ALL_MODELS,
# so the two timestamps are added here instead.
GLOBAL_EXCLUDE = ("created_at", "updated_at")

PARENT_KEYS = frozenset({"trip_id", "order_id", "profile"})


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


def _reservation_parents(reservation: Model) -> dict:
    return {"trip_id": reservation.pk, "order_id": reservation.lead_id}


def _reservation_label(reservation: Model) -> str:
    return reservation.reference


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
    ),
]

_BY_LABEL = {spec.model.lower(): spec for spec in SPECS}


def spec_for(model_or_instance: type[Model] | Model) -> ModelSpec | None:
    return _BY_LABEL.get(model_or_instance._meta.label_lower)


def effective_exclude(model_or_instance: type[Model] | Model) -> tuple[str, ...]:
    """Every field the trail leaves out for this model: its spec's, plus the timestamps."""
    spec = spec_for(model_or_instance)
    return (spec.exclude if spec else ()) + GLOBAL_EXCLUDE


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


def register_all() -> None:
    for spec in SPECS:
        model = apps.get_model(spec.model)
        auditlog.register(
            model,
            include_fields=_own_columns(model),
            exclude_fields=list(effective_exclude(model)),
            mapping_fields=dict(spec.labels),
            m2m_fields=set(spec.m2m),
        )
