"""Entries for changes made by a queryset UPDATE (APC-140, spec §5.1).

A conditional `.update()` (a customer stamp, a hold transition) bypasses the save signals, so
the caller that won the UPDATE records it here, with the values it wrote.
"""

from __future__ import annotations

import copy

from auditlog.context import auditlog_disabled
from auditlog.diff import get_field_value
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model
from django.utils.encoding import smart_str

from . import registry
from .models import AuditEntry


def record_update(instance: Model, **new_values) -> AuditEntry | None:
    """Log a change made by a queryset UPDATE. Old values are read from `instance`, so call it
    before refreshing or assigning; fields the registry excludes are dropped, and nothing is
    written when no tracked field differs (or the model isn't registered)."""
    if auditlog_disabled.get() or registry.spec_for(instance) is None:
        return None
    excluded = set(registry.effective_exclude(instance))
    after = copy.copy(instance)
    changes: dict[str, list[str]] = {}
    for name, value in new_values.items():
        if name in excluded:
            continue
        try:
            field = instance._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        if not field.concrete:
            continue
        setattr(after, field.name, value)
        old = smart_str(get_field_value(instance, field))
        new = smart_str(get_field_value(after, field))
        if old != new:
            changes[name] = [old, new]
    if not changes:
        return None
    return AuditEntry.objects.log_create(instance, action=AuditEntry.Action.UPDATE, changes=changes)
