"""The shared base of the Contacts / Vendors import resources (APC-141, spec §3.4, §7).

Every expected row failure is a `ValidationError` (the library reports the row INVALID and
the other rows still commit). The library's per-row `atomic` block does not roll back when it
catches one, so a screen parses, matches and validates in `before_import_row`, the loader and
`after_init_instance`, and writes only in `before_save_instance` / `after_save_instance` /
`save_m2m`.
"""

from typing import ClassVar

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from import_export.resources import ModelResource
from import_export.results import RowResult
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display

from .xlsx import ROW_COLUMN, Column

OUTCOMES = {
    RowResult.IMPORT_TYPE_NEW: "new",
    RowResult.IMPORT_TYPE_UPDATE: "updated",
    RowResult.IMPORT_TYPE_SKIP: "unchanged",
    RowResult.IMPORT_TYPE_INVALID: "error",
}


def row_no(row) -> int:
    """The Excel row number the reader stamped on a dataset row."""
    return int(row[ROW_COLUMN])


def cell(row, column: str) -> str:
    return row.get(column, "") or ""


def same_number(stored: str, typed: str, e164: str | None) -> bool:
    """`typed` is `stored` as typed, as E.164 or as `phone_display` shows it."""
    return bool(stored) and (typed == stored or e164 == stored or typed == phone_display(stored))


def export_phone(stored: str | None) -> str:
    """A phone for the export: an E.164 number formatted for reading, anything else exactly
    as stored, so a legacy raw number still matches on re-import (`202-555-0143` written
    as `(202) 555-0143` would match nothing and create a duplicate)."""
    if not stored:
        return ""
    return phone_display(stored) if to_e164(stored) == stored else stored


def phone_match_warning(record) -> str:
    """A row with no email matched by phone a record that has one: shared numbers are common
    (family, a front desk), so the preview asks the user to check (spec §5.2 step 4, §6.2)."""
    return f"Matched by phone to {record.name} ({record.email}) — check this is the same person"


def same_text(stored, typed) -> bool:
    """`stored` and `typed` differ only in what a workbook can't carry: line endings, outer
    whitespace, or a control character Excel refuses (the export drops it)."""
    return isinstance(stored, str) and isinstance(typed, str) and _text(stored) == _text(typed)


def _text(value: str) -> str:
    value = ILLEGAL_CHARACTERS_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return value.strip()


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class PortalResource(ModelResource):
    """Blank never clears, `changes` per row, duplicate keys in the file, §3.4 outcomes."""

    COLUMNS: ClassVar[tuple[Column, ...]] = ()

    class Meta:
        use_bulk = False  # save() must run: Contact.save, billing + audit-log signals
        use_transactions = True
        skip_unchanged = True
        report_skipped = True
        skip_diff = False
        skip_html_diff = True
        store_instance = True
        clean_model_instances = True
        import_id_fields = []  # the screen's loader decides; ["id"] would bypass it

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.notes: dict[int, dict] = {}
        self.duplicates: dict[int, str] = {}
        self._created: set[int] = set()
        self._parsed: dict = {}
        self._dry_run = False

    @classmethod
    def export_queryset(cls, queryset):
        """`queryset` with what the export's dehydrate methods read joined or prefetched."""
        return queryset

    def before_import(self, dataset, **kwargs) -> None:
        """Start a run (one resource may preview, then apply), then one pass over the file:
        a later row with an earlier row's key is an error.

        The key is what the row matches by — the email (case-insensitive) when there is one,
        else the phone (by E.164, or as typed when it doesn't parse).
        """
        self.notes, self.duplicates, self._created = {}, {}, set()
        first: dict[tuple[str, str], int] = {}
        for values in dataset.dict:
            email = cell(values, "Email").strip().lower()
            phone = cell(values, "Phone").strip()
            if email:
                key = ("email", email)
            elif phone:
                key = ("phone", to_e164(phone) or phone)
            else:
                continue
            if key in first:
                self.duplicates[row_no(values)] = f"Same {key[0]} as row {first[key]}"
            else:
                first[key] = row_no(values)

    def before_import_row(self, row, **kwargs) -> None:
        n = row_no(row)
        self.notes[n] = {"key": "", "warnings": [], "changes": []}
        self._parsed = {}
        self._dry_run = bool(kwargs.get("dry_run"))
        if n in self.duplicates:
            raise ValidationError(self.duplicates[n])

    def after_init_instance(self, instance, new, row, **kwargs) -> None:
        self._parsed["new"] = new

    def after_save_instance(self, instance, row, **kwargs) -> None:
        if self._parsed.get("new"):
            self._created.add(instance.pk)

    def import_field(self, field, instance, row, is_m2m=False, **kwargs):
        """A blank cell leaves the saved value alone: an import never clears. Text equal to
        the stored value but for line endings, outer whitespace or a control character is
        left alone too (`same_text`)."""
        if not field.attribute or _blank(row.get(field.column_name)):
            return
        if not is_m2m and same_text(
            getattr(instance, field.attribute, None), row.get(field.column_name)
        ):
            return  # the stored text, as the export wrote it: not a change, never rewritten
        super().import_field(field, instance, row, is_m2m, **kwargs)

    def skip_row(self, instance, original, row, import_validation_errors=None) -> bool:
        if import_validation_errors:
            return False
        note = self.notes[row_no(row)]
        if original.pk is None:
            note["changes"] = self.new_changes(instance, row)
            return False
        note["changes"] = self.model_changes(original, instance) + self.virtual_changes(
            instance, row
        )
        return not note["changes"]

    def _model_fields(self):
        return [f for f in self.get_import_fields() if f.attribute]

    def _display(self, instance, field) -> str:
        """A model column as the user reads it: choice labels, not codes; None as blank."""
        value = field.get_value(instance)
        if value is None:
            return ""
        model_field = instance._meta.get_field(field.attribute)
        if model_field.choices:
            return str(dict(model_field.flatchoices).get(value, value))
        return str(value)

    def new_changes(self, instance, row) -> list[list[str]]:
        """A new row lists every non-blank cell, in column order."""
        model_fields = {f.column_name: f for f in self._model_fields()}
        changes = []
        for column in self.COLUMNS:
            value = cell(row, column.name)
            if _blank(value):
                continue
            field = model_fields.get(column.name)
            shown = (
                self._display(instance, field)
                if field
                else self.display_virtual(column.name, value)
            )
            changes.append([column.name, "", shown])
        return changes

    def display_virtual(self, column: str, value: str) -> str:
        """A non-blank virtual cell as a new row's change shows it; a screen may format it."""
        return value.strip()

    def model_changes(self, original, instance) -> list[list[str]]:
        changes = []
        for field in self._model_fields():
            if field.get_value(original) != field.get_value(instance):
                changes.append(
                    [
                        field.column_name,
                        self._display(original, field),
                        self._display(instance, field),
                    ]
                )
        return changes

    def virtual_changes(self, instance, row) -> list[list[str]]:
        """Changes in the columns with no model attribute; a screen with any overrides this."""
        return []

    def outcome(self, row_result, values: dict) -> dict:
        """The §3.4 dict for one row; `values` is the dataset row as uploaded."""
        n = row_no(values)
        note = self.notes.get(n, {})
        kind = OUTCOMES[row_result.import_type]
        instance = row_result.instance
        label = cell(values, "Name").strip() or (getattr(instance, "name", "") or "")
        key = note.get("key") or cell(values, "Email").strip() or cell(values, "Phone").strip()
        out = {"row": n, "outcome": kind, "label": label, "key": key}
        if kind == "error":
            columns = {f.attribute: f.column_name for f in self._model_fields()}
            return out | {
                "object_id": None,
                "changes": [],
                "warnings": [],
                "reason": self.describe(row_result.validation_error, columns),
                "values": {k: v for k, v in values.items() if k != ROW_COLUMN},
            }
        # A dry run rolls back what it created: no link may point at it.
        created_here = self._dry_run and row_result.object_id in self._created
        object_id = None if created_here else row_result.object_id
        return out | {
            "object_id": object_id,
            "changes": note.get("changes", []),
            "warnings": note.get("warnings", []),
        }

    @staticmethod
    def describe(error: ValidationError, columns: dict[str, str]) -> str:
        """One sentence per message, field messages prefixed with the column the user sees."""
        if not hasattr(error, "error_dict"):
            return " ".join(error.messages)
        parts = []
        for name, messages in error.message_dict.items():
            for message in messages:
                if name == NON_FIELD_ERRORS:
                    parts.append(message)
                else:
                    parts.append(f"{columns.get(name, name)}: {message}")
        return " ".join(parts)
