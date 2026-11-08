"""The audit frame: who and through what, resolved only when an entry is written."""

import sys
from unittest.mock import Mock

import pytest
from django.contrib.contenttypes.models import ContentType

from apps.accounts.factories import UserFactory
from apps.audit.context import audit_context, current_cid
from apps.audit.models import AuditEntry
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _entries(*reservations):
    """The trips' own entries: by content type too, since pks repeat across tables."""
    ct = ContentType.objects.get_for_model(Reservation)
    return AuditEntry.objects.filter(content_type=ct, object_id__in=[r.pk for r in reservations])


def test_nested_context_restores_outer_frame():
    with audit_context(source="outer", cid="c-outer"):
        with audit_context(source="inner"):
            pass
        reservation = ReservationFactory()

    entry = _entries(reservation).get()
    assert entry.source == "outer"
    assert entry.cid == "c-outer"


def test_callable_actor_not_evaluated_when_nothing_written():
    reservation = ReservationFactory()
    actor = Mock(return_value=None)

    with audit_context(actor=actor, source="portal"):
        reservation.save()

    assert not actor.called
    assert _entries(reservation).count() == 1  # the create only


def test_callable_actor_evaluated_once_per_frame():
    user = UserFactory()
    actor = Mock(return_value=user)

    with audit_context(actor=actor, source="portal"):
        first = ReservationFactory()
        second = ReservationFactory()

    assert actor.call_count == 1
    entries = _entries(first, second)
    assert [(e.actor, e.actor_email) for e in entries] == [(user, user.email)] * 2


def test_callable_source_with_no_value_falls_back(monkeypatch):
    """A frame must never write an entry with neither actor nor source."""
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest", "-q"])

    with audit_context(source=Mock(return_value=None)):
        reservation = ReservationFactory()

    entry = _entries(reservation).get()
    assert entry.source == "system"
    assert entry.actor is None


def test_empty_source_falls_back(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["manage.py", "shell"])

    with audit_context(source=""):
        reservation = ReservationFactory()

    assert _entries(reservation).get().source == "command: shell"


def test_callable_source_evaluated_once_per_frame():
    source = Mock(return_value="portal")

    with audit_context(source=source):
        first = ReservationFactory()
        second = ReservationFactory()

    assert source.call_count == 1
    assert [e.source for e in _entries(first, second)] == ["portal", "portal"]


def test_no_frame_under_manage_py_is_command_source(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["manage.py", "backfill_trip_timezones"])

    reservation = ReservationFactory()

    entry = _entries(reservation).get()
    assert entry.source == "command: backfill_trip_timezones"
    assert entry.actor is None


def test_no_frame_otherwise_is_system(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest", "-q"])

    reservation = ReservationFactory()

    entry = _entries(reservation).get()
    assert entry.source == "system"
    assert entry.actor is None


def test_no_frame_has_no_cid(settings):
    assert settings.AUDITLOG_CID_HEADER is None
    assert current_cid() is None


def test_a_source_longer_than_its_column_is_clipped_and_the_save_still_succeeds():
    """An import names its file in the source (§4.4); a 255-character filename must not fail
    the business row's save on a strict database."""
    long_source = "import: " + "x" * 292
    assert len(long_source) == 300

    with audit_context(source=long_source):
        reservation = ReservationFactory()

    entry = _entries(reservation).get()
    max_length = AuditEntry._meta.get_field("source").max_length
    assert max_length == 255
    assert entry.source == long_source[:max_length]
