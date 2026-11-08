"""The audit frame: who and through what, resolved only when an entry is written."""

import sys
from unittest.mock import Mock

import pytest

from apps.accounts.factories import UserFactory
from apps.audit.context import audit_context, current_cid
from apps.audit.models import AuditEntry
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def test_nested_context_restores_outer_frame():
    with audit_context(source="outer", cid="c-outer"):
        with audit_context(source="inner"):
            pass
        reservation = ReservationFactory()

    entry = AuditEntry.objects.get(object_id=reservation.pk)
    assert entry.source == "outer"
    assert entry.cid == "c-outer"


def test_callable_actor_not_evaluated_when_nothing_written():
    reservation = ReservationFactory()
    actor = Mock(return_value=None)

    with audit_context(actor=actor, source="portal"):
        reservation.save()

    assert not actor.called
    assert AuditEntry.objects.filter(object_id=reservation.pk).count() == 1  # the create only


def test_callable_actor_evaluated_once_per_frame():
    user = UserFactory()
    actor = Mock(return_value=user)

    with audit_context(actor=actor, source="portal"):
        first = ReservationFactory()
        second = ReservationFactory()

    assert actor.call_count == 1
    entries = AuditEntry.objects.filter(object_id__in=[first.pk, second.pk])
    assert [(e.actor, e.actor_email) for e in entries] == [(user, user.email)] * 2


def test_no_frame_under_manage_py_is_command_source(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["manage.py", "backfill_trip_timezones"])

    reservation = ReservationFactory()

    entry = AuditEntry.objects.get(object_id=reservation.pk)
    assert entry.source == "command: backfill_trip_timezones"
    assert entry.actor is None


def test_no_frame_otherwise_is_system(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/usr/bin/pytest", "-q"])

    reservation = ReservationFactory()

    entry = AuditEntry.objects.get(object_id=reservation.pk)
    assert entry.source == "system"
    assert entry.actor is None


def test_no_frame_has_no_cid(settings):
    assert settings.AUDITLOG_CID_HEADER is None
    assert current_cid() is None
