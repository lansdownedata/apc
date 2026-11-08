"""Contacts / vendors workbook import + export (APC-141).

Preview runs the whole file as a dry run and stores the row outcomes; apply re-reads the stored
file and runs it again for real, so it re-validates against the data as it is then (spec §3.1).
"""

import io
import logging
from collections import Counter
from datetime import timedelta

import tablib
from django.db import transaction
from django.utils import timezone

from .models import ImportRun
from .screens import Screen, get_screen
from .xlsx import ROW_COLUMN, FileError, ParsedSheet, read_rows, write_workbook

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1_000
PREVIEW_TTL = timedelta(hours=24)
FILE_RETENTION = timedelta(days=3)

TOO_BIG = (
    "This file is over 2 MB. Import at most 1,000 rows at a time — split it into smaller files."
)
TOO_MANY_ROWS = (
    "This file has {n:,} rows. Import at most 1,000 at a time — split it into smaller files."
)
NO_ROWS = "This file has no rows to import."
UPLOAD_MISSING = "The uploaded file couldn't be found. Upload it again."
ROW_CRASHED = "Something went wrong reading row {row}. Nothing was saved."
FILE_CRASHED = "Something went wrong reading this file. Nothing was saved."
ALREADY_APPLIED = "This import was already applied."
EXPIRED = "This preview has expired. Upload the file again."
NOT_APPLICABLE = "This import failed. Upload the file again."


class ApplyRefused(Exception):
    """The run can't be applied; str(e) is the user-facing sentence."""


class _Crashed(Exception):
    """The import hit an unexpected error and rolled back; str(e) is the run's error."""


def _fail(run: ImportRun, error: str) -> None:
    run.status = ImportRun.Status.FAILED
    run.error = error
    run.save(update_fields=["status", "error", "updated_at"])


def _read(run: ImportRun, screen: Screen) -> ParsedSheet:
    """The stored workbook, parsed. OSError when the object is gone from storage."""
    with run.file.open("rb") as stored:
        content = io.BytesIO(stored.read())
    return read_rows(content, sheet=screen.sheet, columns=screen.columns)


def _counts(rows: list[dict], examples: int) -> dict[str, int]:
    tally = Counter(row["outcome"] for row in rows)
    return {
        "new": tally["new"],
        "updated": tally["updated"],
        "unchanged": tally["unchanged"],
        "errors": tally["error"],
        "examples": examples,
    }


def _import(run: ImportRun, screen: Screen, parsed: ParsedSheet, *, dry_run: bool) -> list[dict]:
    """Run the import; return the §3.4 row outcomes with the reader's row errors, by row.

    An unexpected error rolls the whole import back (the library does it for an ERROR row;
    an exception escaping `import_data` unwinds its transaction) and raises `_Crashed`.
    """
    dataset = parsed.dataset
    resource = screen.resource()
    try:
        result = resource.import_data(
            dataset,
            dry_run=dry_run,
            use_transactions=True,
            rollback_on_validation_errors=False,
        )
    except Exception as exc:
        logger.exception("Import run %s failed", run.pk)
        raise _Crashed(FILE_CRASHED) from exc
    if result.has_errors():
        crashed = result.row_errors()
        if crashed:
            number, errors = crashed[0]
            row = dataset[number - 1][dataset.headers.index(ROW_COLUMN)]
            logger.exception(
                "Import run %s failed on row %s", run.pk, row, exc_info=errors[0].error
            )
            raise _Crashed(ROW_CRASHED.format(row=row))
        logger.exception("Import run %s failed", run.pk, exc_info=result.base_errors[0].error)
        raise _Crashed(FILE_CRASHED)
    outcomes = [
        resource.outcome(row_result, dict(zip(dataset.headers, values, strict=True)))
        for row_result, values in zip(result.rows, dataset, strict=True)
    ]
    return sorted(outcomes + parsed.row_errors, key=lambda row: row["row"])


def preview(run: ImportRun) -> None:
    """Dry-run the uploaded file and store its outcomes; a file problem fails the run."""
    screen = get_screen(run.screen)
    try:
        if run.file.size > MAX_BYTES:
            return _fail(run, TOO_BIG)
        parsed = _read(run, screen)
    except FileError as exc:
        return _fail(run, str(exc))
    except OSError:  # the stored object is gone
        logger.warning("Import run %s: uploaded file missing from storage", run.pk)
        return _fail(run, UPLOAD_MISSING)
    total = len(parsed.dataset) + len(parsed.row_errors)
    if total > MAX_ROWS:
        return _fail(run, TOO_MANY_ROWS.format(n=total))
    if total == 0:
        return _fail(run, NO_ROWS)
    try:
        rows = _import(run, screen, parsed, dry_run=True)
    except _Crashed as exc:
        return _fail(run, str(exc))
    run.rows = rows
    run.preview_counts = _counts(rows, parsed.examples) | {
        "ignored_columns": parsed.ignored_columns
    }
    run.status = ImportRun.Status.PREVIEWED
    run.error = ""
    run.save(update_fields=["rows", "preview_counts", "status", "error", "updated_at"])


def apply(run_pk: int) -> ImportRun:
    """Run a previewed import for real, once. Holds the run's row lock throughout, so a
    double-click or a second tab gets "already applied" (spec §7)."""
    with transaction.atomic():
        run = ImportRun.objects.select_for_update().get(pk=run_pk)
        if run.status == ImportRun.Status.APPLIED:
            raise ApplyRefused(ALREADY_APPLIED)
        if run.status != ImportRun.Status.PREVIEWED:
            raise ApplyRefused(NOT_APPLICABLE)
        if run.is_expired or not run.file:
            raise ApplyRefused(EXPIRED)
        screen = get_screen(run.screen)
        try:
            parsed = _read(run, screen)
        except OSError as exc:  # gone from storage
            raise ApplyRefused(EXPIRED) from exc
        except FileError as exc:
            _fail(run, str(exc))
            return run
        try:
            rows = _import(run, screen, parsed, dry_run=False)
        except _Crashed as exc:
            _fail(run, str(exc))
            return run
        previewed = {row["row"]: row["outcome"] for row in run.rows}
        for row in rows:
            before = previewed.get(row["row"])
            if before is not None and before != row["outcome"]:
                row.update(differs=True, preview_outcome=before)
        run.rows = rows
        run.counts = _counts(rows, parsed.examples)
        run.applied_at = timezone.now()
        run.status = ImportRun.Status.APPLIED
        run.save(update_fields=["rows", "counts", "applied_at", "status", "updated_at"])
    return run


def export_rows(screen: Screen, filters, user) -> tuple[ImportRun, bytes]:
    """The rows the screen shows for `filters`, as a workbook; every export is logged.

    The rows are evaluated once, as a list: handed a prefetched queryset, the library pages
    it by IMPORT_EXPORT_CHUNK_SIZE (a COUNT, a page and a prefetch per chunk), so the query
    count would grow with N and offset paging could skip or repeat a row edited mid-export.
    """
    filters = screen.clean_filters(filters)
    dataset = screen.resource().export(queryset=list(screen.queryset(filters)))
    content = write_workbook(screen.spec(), dataset)
    run = ImportRun.objects.create(
        kind=ImportRun.Kind.EXPORT,
        status=ImportRun.Status.APPLIED,
        screen=screen.slug,
        user=user,
        filename=f"{screen.slug}-{timezone.localdate():%Y-%m-%d}.xlsx",
        filters=filters,
        counts={"rows": len(dataset)},
        applied_at=timezone.now(),
    )
    return run, content


def error_rows(run: ImportRun) -> bytes:
    """The rows that could not be applied, as uploaded, with a last "Reason" column. Built
    from `run.rows`, never from the file, so it works after the file is purged."""
    screen = get_screen(run.screen)
    names = [column.name for column in screen.columns]
    dataset = tablib.Dataset(headers=[*names, "Reason"])
    for row in run.rows:
        if row["outcome"] == "error":
            values = row.get("values", {})
            dataset.append([values.get(name, "") for name in names] + [row.get("reason", "")])
    return write_workbook(screen.spec(), dataset)


def template_bytes(screen: Screen) -> bytes:
    """Header, example rows and the Accepted values sheet (spec §4.4)."""
    names = [column.name for column in screen.columns]
    dataset = tablib.Dataset(*screen.examples(), headers=names)
    return write_workbook(screen.spec(), dataset)


def purge_files() -> int:
    """Delete the stored workbook of every run older than FILE_RETENTION; keep the run rows.

    Idempotent. A file already gone from storage is not an error. Returns the count purged.
    """
    cutoff = timezone.now() - FILE_RETENTION
    runs = ImportRun.objects.filter(created_at__lt=cutoff).exclude(file="")
    purged = 0
    for run in runs:
        try:
            run.file.delete(save=False)
        except OSError:  # includes FileNotFoundError: the object is already gone
            logger.warning("Import run %s: stored file already missing", run.pk)
        ImportRun.objects.filter(pk=run.pk).update(file="")
        purged += 1
    return purged
