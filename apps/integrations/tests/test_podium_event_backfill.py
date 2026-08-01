"""The event_uid backfill must survive the retry duplicates already in production.

It runs in Heroku's release phase, so an IntegrityError here aborts the whole deploy.
Prod holds 8 rows sharing one eventUid from the 2026-07-31 incident.
"""

from importlib import import_module

import pytest
from django.apps import apps as django_apps

from apps.integrations.models import PodiumEvent

# Module name starts with a digit, so it can't be imported with `from ... import`.
_0005 = import_module("apps.integrations.migrations.0005_backfill_podium_event_uid")

pytestmark = pytest.mark.django_db


def _event(event_uid: str | None):
    payload = {"data": {"uid": "m"}, "metadata": {"eventType": "message.failed"}}
    if event_uid:
        payload["metadata"]["eventUid"] = event_uid
    return PodiumEvent.objects.create(event_type="message.failed", payload=payload)


def test_backfill_stamps_first_row_and_leaves_duplicates_null():
    first = _event("dup-uid")
    second = _event("dup-uid")
    third = _event("dup-uid")
    other = _event("other-uid")

    _0005.forwards(django_apps, None)

    for row in (first, second, third, other):
        row.refresh_from_db()
    assert first.event_uid == "dup-uid"
    assert second.event_uid is None, "later duplicates must stay NULL, not raise"
    assert third.event_uid is None
    assert other.event_uid == "other-uid"


def test_backfill_skips_rows_without_metadata():
    legacy = PodiumEvent.objects.create(
        event_type="message.received", payload={"eventType": "message.received"}
    )

    _0005.forwards(django_apps, None)

    legacy.refresh_from_db()
    assert legacy.event_uid is None


def test_backfill_is_rerunnable():
    _event("stable-uid")

    _0005.forwards(django_apps, None)
    _0005.forwards(django_apps, None)

    assert PodiumEvent.objects.filter(event_uid="stable-uid").count() == 1
