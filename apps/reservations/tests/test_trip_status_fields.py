import pytest

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def test_new_reservation_fields_default_empty():
    trip = ReservationFactory()
    assert trip.needs_farm_out is False
    assert trip.circling_at is None
    assert trip.circling_source == ""
    assert trip.circling_by is None
    assert trip.cancel_reason == ""
    assert trip.cancel_covid is False
    assert trip.cancelled_at is None
    assert trip.cancelled_by is None


def test_sent_back_is_not_active():
    assert "sent_back" in Assignment.Status.values
    assert Assignment.Status.SENT_BACK not in Assignment.ACTIVE_STATUSES
    sent_back = AssignmentFactory(status=Assignment.Status.SENT_BACK)
    assert not Assignment.objects.active().filter(pk=sent_back.pk).exists()
    assert sent_back.offer_accepted_at is None
    assert sent_back.sent_back_reason == ""
    assert sent_back.sent_back_by is None
