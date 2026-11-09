from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.core import cron
from apps.imports import services
from apps.imports.factories import ImportRunFactory
from apps.imports.models import ImportRun

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _run_with_file(age: timedelta) -> ImportRun:
    run = ImportRunFactory(rows=[{"row": 2}], counts={"new": 1})
    run.file.save("contacts.xlsx", ContentFile(b"xlsx"), save=True)
    ImportRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - age)
    run.refresh_from_db()
    return run


def test_purge_deletes_old_files_and_keeps_rows():
    run = _run_with_file(timedelta(days=4))
    name = run.file.name
    assert services.purge_files() == 1
    run.refresh_from_db()
    assert run.file == ""
    assert not default_storage.exists(name)
    assert run.rows == [{"row": 2}]
    assert run.counts == {"new": 1}


def test_purge_leaves_new_runs_alone():
    run = _run_with_file(timedelta(days=2))
    assert services.purge_files() == 0
    run.refresh_from_db()
    assert run.file != ""
    assert default_storage.exists(run.file.name)


def test_purge_tolerates_missing_object():
    run = _run_with_file(timedelta(days=4))
    default_storage.delete(run.file.name)
    assert services.purge_files() == 1
    run.refresh_from_db()
    assert run.file == ""


def test_purge_job_is_registered():
    assert cron.JOBS["purge-import-files"] is services.purge_files
