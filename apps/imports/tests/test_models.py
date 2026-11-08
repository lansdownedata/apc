from datetime import timedelta

import pytest
from django.utils import timezone

from apps.imports.factories import ImportRunFactory
from apps.imports.models import ImportRun

pytestmark = pytest.mark.django_db


def _age(run: ImportRun, delta: timedelta) -> ImportRun:
    ImportRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - delta)
    run.refresh_from_db()
    return run


def test_preview_expires_after_24_hours():
    old = _age(ImportRunFactory(), timedelta(hours=25))
    fresh = _age(ImportRunFactory(), timedelta(hours=23))
    applied = _age(ImportRunFactory(status=ImportRun.Status.APPLIED), timedelta(hours=25))
    assert old.is_expired
    assert not fresh.is_expired
    assert not applied.is_expired
