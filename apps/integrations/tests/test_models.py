from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.integrations.factories import (
    PodiumCredentialFactory,
    PodiumEventFactory,
    ZapEventFactory,
)
from apps.integrations.models import PodiumCredential, ZapEvent

pytestmark = pytest.mark.django_db


# --- PodiumCredential ------------------------------------------------------
def test_credential_not_expired_in_future():
    cred = PodiumCredentialFactory(expires_at=timezone.now() + timedelta(hours=1))
    assert cred.is_expired is False
    assert cred.needs_refresh is False


def test_credential_expired_in_past():
    cred = PodiumCredentialFactory(expires_at=timezone.now() - timedelta(minutes=1))
    assert cred.is_expired is True
    assert cred.needs_refresh is True


def test_credential_needs_refresh_within_buffer():
    cred = PodiumCredentialFactory(expires_at=timezone.now() + timedelta(seconds=30))
    assert cred.is_expired is False
    assert cred.needs_refresh is True


def test_current_returns_latest_credential():
    PodiumCredentialFactory()
    latest = PodiumCredentialFactory()
    assert PodiumCredential.current() == latest


# --- ZapEvent --------------------------------------------------------------
def test_zap_event_succeeded():
    assert ZapEventFactory(result=ZapEvent.Result.SUCCESS).succeeded is True
    assert ZapEventFactory(result=ZapEvent.Result.ERROR).succeeded is False


def test_zap_event_idempotency_key_unique():
    ZapEventFactory(idempotency_key="dup")
    with pytest.raises(IntegrityError):
        ZapEventFactory(idempotency_key="dup")


# --- PodiumEvent -----------------------------------------------------------
def test_podium_event_mark_processed():
    event = PodiumEventFactory(processed=False)
    event.mark_processed()
    event.refresh_from_db()
    assert event.processed is True
