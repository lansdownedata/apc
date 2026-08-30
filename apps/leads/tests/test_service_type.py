"""ServiceType — the configurable catalog behind a trip's "Service".

Replaces the free-text `Reservation.service`, which let every agent invent their own
wording for the same six jobs. The catalog is edited in Settings and is the single
source for both the reservation editor and the public booking widget's occasion picker.
"""

import pytest
from django.db import IntegrityError, transaction

from apps.leads.factories import ServiceTypeFactory
from apps.leads.models import ServiceType
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

SEEDED_NAMES = {
    "Airport Transfer",
    "Corporate Travel",
    "Wedding Transportation",
    "Group / Shuttle Service",
    "Night Out",
    "Other",
}


def test_the_starter_catalog_is_seeded():
    """The occasions the public site already offered, so nothing regresses on day one."""
    assert SEEDED_NAMES <= set(ServiceType.objects.values_list("name", flat=True))


def test_ordering_is_sort_order_then_name():
    names = ["Zebra Run", "Alpha Run", "Mid Run"]
    ServiceTypeFactory(name="Zebra Run", sort_order=1)
    ServiceTypeFactory(name="Alpha Run", sort_order=2)
    ServiceTypeFactory(name="Mid Run", sort_order=1)
    assert [s.name for s in ServiceType.objects.filter(name__in=names)] == [
        "Mid Run",
        "Zebra Run",
        "Alpha Run",
    ]


def test_names_are_unique_case_insensitively():
    ServiceTypeFactory(name="Bachelor Party")
    with transaction.atomic(), pytest.raises(IntegrityError):
        ServiceType.objects.create(name="bachelor party")


def test_a_type_is_active_by_default():
    assert ServiceTypeFactory(name="Brand New").active is True


def test_str_is_the_name():
    assert str(ServiceTypeFactory(name="Prom Night")) == "Prom Night"


# ------------------------------------------------------------ on the reservation


def test_a_reservation_points_at_a_service_type():
    st = ServiceTypeFactory(name="Wine Tour")
    res = ReservationFactory(service_type=st)
    res.refresh_from_db()
    assert res.service_type.name == "Wine Tour"


def test_a_reservation_may_have_no_service_type():
    assert ReservationFactory(service_type=None).service_type is None


def test_retiring_a_type_leaves_historical_trips_readable():
    """SET_NULL, not CASCADE — deleting a catalog row must never delete a booked trip."""
    st = ServiceTypeFactory(name="Discontinued Run")
    res = ReservationFactory(service_type=st)
    st.delete()
    res.refresh_from_db()
    assert res.pk is not None
    assert res.service_type is None


def test_the_free_text_service_field_is_gone():
    assert not hasattr(ReservationFactory(), "service")


def test_the_trip_label_falls_back_to_the_trip_type():
    res = ReservationFactory(service_type=None, trip_type="hourly")
    assert res.service_label == res.get_trip_type_display()


def test_the_trip_label_is_the_service_type_when_set():
    res = ReservationFactory(service_type=ServiceTypeFactory(name="Wine Tour"))
    assert res.service_label == "Wine Tour"
