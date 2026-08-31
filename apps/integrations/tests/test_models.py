from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.integrations import crypto
from apps.integrations.factories import (
    PodiumCredentialFactory,
    PodiumEventFactory,
    ZapEventFactory,
)
from apps.integrations.models import LACustomer, LAEvent, PodiumCredential, ZapEvent

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


# --- LACustomer -----------------------------------------------------------
def test_la_customer_password_round_trip():
    lac = LACustomer.objects.create(
        contact=ContactFactory(),
        la_customer_id="12345",
        la_account_number="99119924",
        email_used="jane@example.com",
        password_encrypted=crypto.encrypt("s3cret-pw"),
    )
    assert lac.password == "s3cret-pw"
    assert "s3cret-pw" not in lac.password_encrypted


# --- ZapEvent (extended) ---------------------------------------------------
def test_zap_event_preview_result_exists():
    assert ZapEvent.Result.PREVIEW == "preview"


# --- LAEvent ---------------------------------------------------------------
def test_la_event_str():
    event = LAEvent.objects.create(event="reservation.booked", payload={"id": 1})
    assert "reservation.booked" in str(event)


def test_calendly_event_key_is_unique_so_retries_cannot_double_insert(db):
    """Calendly re-delivers until it sees a timely 2xx. The DB, not app logic, is
    what stops two in-flight retries from both creating a Lead."""
    from django.db import transaction

    from apps.integrations.models import CalendlyEvent

    key = "invitee.created:https://api.calendly.com/scheduled_events/e1/invitees/i1"
    CalendlyEvent.objects.create(
        idempotency_key=key, event_type=CalendlyEvent.EventType.INVITEE_CREATED
    )
    # atomic() so the broken transaction is rolled back to a savepoint rather than
    # poisoning the rest of the test — the same reason the processor wraps its insert.
    with pytest.raises(IntegrityError), transaction.atomic():
        CalendlyEvent.objects.create(
            idempotency_key=key, event_type=CalendlyEvent.EventType.INVITEE_CREATED
        )


def test_calendly_event_mark_processed(db):
    from apps.integrations.models import CalendlyEvent

    event = CalendlyEvent.objects.create(
        idempotency_key="k1", event_type=CalendlyEvent.EventType.INVITEE_CANCELED
    )
    event.mark_processed()
    event.refresh_from_db()
    assert event.processed is True


def test_calendly_event_survives_its_lead_being_deleted(db):
    """The raw payload is an audit record; losing the Lead must not lose it."""
    from apps.integrations.models import CalendlyEvent
    from apps.leads.factories import LeadFactory

    lead = LeadFactory()
    event = CalendlyEvent.objects.create(
        idempotency_key="k2", event_type=CalendlyEvent.EventType.INVITEE_CREATED, lead=lead
    )
    lead.delete()
    event.refresh_from_db()
    assert event.lead is None
    assert event.payload == {}
