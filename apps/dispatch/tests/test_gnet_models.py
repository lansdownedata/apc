"""Tests for GNet farm-out data model."""

import pytest
from django.db import IntegrityError, transaction

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, GnetEvent
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def test_vendor_without_gnet_grid_id_is_not_gnet_capable():
    """A vendor with blank gnet_grid_id should not be GNet-capable."""
    vendor = VendorFactory(gnet_grid_id="")
    assert not vendor.is_gnet_capable


def test_vendor_with_whitespace_gnet_grid_id_is_not_gnet_capable():
    """A vendor with only whitespace in gnet_grid_id should not be GNet-capable."""
    vendor = VendorFactory(gnet_grid_id="   ")
    assert not vendor.is_gnet_capable


def test_vendor_with_gnet_grid_id_is_gnet_capable():
    """A vendor with a valid gnet_grid_id should be GNet-capable."""
    vendor = VendorFactory(gnet_grid_id="abc123")
    assert vendor.is_gnet_capable


def test_assignment_gnet_transaction_id_defaults_blank():
    """Assignment.gnet_transaction_id should default to blank."""
    assignment = AssignmentFactory()
    assert assignment.gnet_transaction_id == ""


def test_assignment_gnet_transaction_id_is_indexed():
    """Every inbound callback correlates on this column — without an index each one
    full-scans the assignment table on the hot path."""
    assert Assignment._meta.get_field("gnet_transaction_id").db_index is True


def test_gnet_event_defaults_to_pending():
    """GnetEvent.result should default to PENDING."""
    assignment = AssignmentFactory()
    event = GnetEvent(
        assignment=assignment,
        action=GnetEvent.Action.SEND_TRIP,
        idempotency_key="test-key-001",
    )
    event.save()
    assert event.result == GnetEvent.Result.PENDING


def test_gnet_event_idempotency_key_is_unique():
    """A second GnetEvent with the same idempotency_key should raise IntegrityError."""
    assignment = AssignmentFactory()

    # Create the first event
    event1 = GnetEvent(
        assignment=assignment,
        action=GnetEvent.Action.SEND_TRIP,
        idempotency_key="unique-key-001",
    )
    event1.save()

    # Try to create a second event with the same idempotency_key
    event2 = GnetEvent(
        assignment=assignment,
        action=GnetEvent.Action.SEND_TRIP,
        idempotency_key="unique-key-001",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            event2.save()
