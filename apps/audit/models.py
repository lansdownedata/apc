"""The audit trail's one table (APC-140).

`AUDITLOG_LOGENTRY_MODEL` points auditlog at `AuditEntry`, so auditlog's own migrations
record without creating `auditlog_logentry` and every entry lands here. The extra columns are
plain values, not foreign keys, so an entry outlives the rows it describes.
"""

from auditlog.cid import get_cid
from auditlog.models import AbstractLogEntry, LogEntryManager
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models

from . import context, registry


class AuditEntryManager(LogEntryManager):
    """Stamps parents, a cheap label and the frame's who/through-what on every entry.

    Everything is `setdefault`, so a caller passing its own value wins. The frame is
    resolved only when an entry is actually written, which keeps a request that changes
    nothing from loading its user.
    """

    def _stamp(self, instance: models.Model, kwargs: dict) -> None:
        for key, value in registry.parents(instance).items():
            kwargs.setdefault(key, value)
        kwargs.setdefault("object_repr", registry.label(instance))
        if not {"actor", "actor_email", "source"} <= kwargs.keys():
            actor, actor_email, source = context.resolve()
            kwargs.setdefault("actor", actor)
            kwargs.setdefault("actor_email", actor_email)
            kwargs.setdefault("source", source)

    @staticmethod
    def _without_equivalents(instance: models.Model, changes: dict) -> dict:
        """Drop pairs that differ only as text: auditlog compares `str()`s, so an editor
        assigning Decimal("185") over a stored 185.00 would read as a change. Only the
        model's own columns are compared; any other key (a reverse relation's name used by an
        explicit entry, e.g. `named_passengers`) is kept as given."""
        kept = {}
        for name, (old, new) in changes.items():
            try:
                field = instance._meta.get_field(name)
                same = field.concrete and field.to_python(old) == field.to_python(new)
            except (FieldDoesNotExist, ValidationError, TypeError, ValueError):
                same = False
            if not same:
                kept[name] = [old, new]
        return kept

    def log_create(self, instance: models.Model, force_log: bool = False, **kwargs):
        changes = kwargs.get("changes")
        if changes and kwargs.get("action") == AbstractLogEntry.Action.UPDATE:
            changes = kwargs["changes"] = self._without_equivalents(instance, changes)
            if not changes and not force_log:
                return None
        if kwargs.get("changes") is None and not force_log:
            return None
        self._stamp(instance, kwargs)
        return self._write(instance, kwargs)

    def _write(self, instance: models.Model, kwargs: dict):
        """`LogEntryManager.log_create`'s body minus its `smart_str(instance)`: that runs
        even when `object_repr` is given, and `Lead.__str__` reads its contact, a query per
        entry (spec §4.2)."""
        pk = self._get_pk_value(instance)
        kwargs.setdefault("content_type", ContentType.objects.get_for_model(instance))
        kwargs.setdefault("object_pk", pk)
        kwargs.setdefault("serialized_data", self._get_serialized_data_or_none(instance))
        if isinstance(pk, int):
            kwargs.setdefault("object_id", pk)
        get_additional_data = getattr(instance, "get_additional_data", None)
        if callable(get_additional_data):
            kwargs.setdefault("additional_data", get_additional_data())
        kwargs.setdefault("cid", get_cid())
        return self.create(**kwargs)

    def log_m2m_changes(
        self, changed_queryset, instance: models.Model, operation: str, field_name: str, **kwargs
    ):
        if changed_queryset:
            self._stamp(instance, kwargs)
        return super().log_m2m_changes(changed_queryset, instance, operation, field_name, **kwargs)


class AuditEntry(AbstractLogEntry):
    source = models.CharField(max_length=80, blank=True, db_index=True)
    trip_id = models.BigIntegerField(null=True, db_index=True)
    order_id = models.BigIntegerField(null=True, db_index=True)
    profile = models.CharField(max_length=40, blank=True, db_index=True)

    objects = AuditEntryManager()

    class Meta(AbstractLogEntry.Meta):
        pass
