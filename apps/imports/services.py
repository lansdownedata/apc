"""Contacts / vendors workbook import + export (APC-141)."""

import logging
from datetime import timedelta

from django.utils import timezone

from .models import ImportRun

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1_000
PREVIEW_TTL = timedelta(hours=24)
FILE_RETENTION = timedelta(days=3)


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
