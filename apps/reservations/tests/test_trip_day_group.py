"""Same-day trip grouping for the customer confirmation (APC-19).

A customer with several trips on one date confirms them together: one email, one link,
one checkbox. The group is keyed by contact + local pickup date and deliberately spans
orders — two separate bookings on the same day are still one confirmation.
"""

from datetime import timedelta

import pytest
from django.core.signing import BadSignature
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import trip_day_group

pytestmark = pytest.mark.django_db


def _future(days=20):
    return (timezone.now() + timedelta(days=days)).date()


def _booked(contact, date, **kw):
    return ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED), pickup_date=date, **kw
    )


# --- the group selector -------------------------------------------------------------


def test_group_spans_separate_orders_for_the_same_customer_and_day():
    contact = ContactFactory()
    day = _future()
    a = _booked(contact, day)
    b = _booked(contact, day)  # a different lead entirely

    assert list(trip_day_group(contact, day)) == [a, b]


def test_group_excludes_other_days_and_other_customers():
    contact = ContactFactory()
    day = _future()
    mine = _booked(contact, day)
    _booked(contact, day + timedelta(days=1))
    _booked(ContactFactory(), day)

    assert list(trip_day_group(contact, day)) == [mine]


def test_group_excludes_cancelled_and_unbooked_trips():
    contact = ContactFactory()
    day = _future()
    live = _booked(contact, day)
    _booked(contact, day, trip_status=Reservation.TripStatus.CANCELLED)
    ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.QUOTED), pickup_date=day
    )

    assert list(trip_day_group(contact, day)) == [live]


def test_group_orders_by_pickup_time():
    from datetime import time

    contact = ContactFactory()
    day = _future()
    late = _booked(contact, day, pickup_time=time(17, 0))
    early = _booked(contact, day, pickup_time=time(6, 30))

    assert list(trip_day_group(contact, day)) == [early, late]


# --- the day-group token ------------------------------------------------------------


def test_day_token_round_trips_contact_and_date():
    contact = ContactFactory()
    day = _future()

    got_contact, got_day = ack.read_trip_day_ack_token(ack.make_trip_day_ack_token(contact, day))

    assert got_contact == contact
    assert got_day == day


def test_day_token_rejects_a_tampered_payload():
    token = ack.make_trip_day_ack_token(ContactFactory(), _future())

    with pytest.raises(BadSignature):
        ack.read_trip_day_ack_token(token[:-4] + "zzzz")


def test_day_token_is_not_interchangeable_with_the_affiliate_salt():
    """Separate salts so a leaked trip link can never stand in for another family."""
    from django.core import signing

    foreign = signing.dumps({"contact": 1, "date": "2027-01-01"}, salt="apc.affiliate-ack.v1")

    with pytest.raises(BadSignature):
        ack.read_trip_day_ack_token(foreign)
