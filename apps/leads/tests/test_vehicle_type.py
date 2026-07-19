"""VehicleType — the client assigns types, not individual vehicles.

Note: migration reservations/0003_seed_vehicles pre-seeds six types into every test
database, and VehicleTypeFactory uses django_get_or_create=("name",). Tests here use
names outside that seed list so they exercise creation rather than silently fetching a
seeded row, and scope their queries so seeded rows don't leak into assertions.
"""

import pytest

from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType

pytestmark = pytest.mark.django_db

SEEDED_NAMES = {
    "Luxury Sedan",
    "Luxury SUV",
    "Sprinter Van",
    "Mini Coach",
    "Motor Coach",
    "Stretch Limousine",
}


def test_has_image_description_and_sort_order():
    vt = VehicleTypeFactory(
        name="Vintage Trolley", description="Seats 20 under a wooden roof.", sort_order=4
    )
    assert vt.pk is not None
    assert vt.name not in SEEDED_NAMES, "must create, not fetch a seeded row"
    assert not vt.image, "image is optional and blank by default"
    assert vt.description == "Seats 20 under a wooden roof."
    assert vt.sort_order == 4


def test_description_and_sort_order_default_to_empty_and_zero():
    vt = VehicleTypeFactory(name="Party Bus")
    assert vt.description == ""
    assert vt.sort_order == 0


def test_klass_field_is_gone():
    assert not hasattr(VehicleType(), "klass")


def test_ordering_is_sort_order_then_name():
    names = ["Zebra Coach", "Alpha Sedan", "Mid Van"]
    VehicleTypeFactory(name="Zebra Coach", sort_order=1)
    VehicleTypeFactory(name="Alpha Sedan", sort_order=2)
    VehicleTypeFactory(name="Mid Van", sort_order=1)
    ordered = VehicleType.objects.filter(name__in=names)
    assert [v.name for v in ordered] == ["Mid Van", "Zebra Coach", "Alpha Sedan"]


def test_seeded_types_survived_the_rename():
    """The pre-rename data migration's rows must still be readable as VehicleType."""
    assert SEEDED_NAMES <= set(VehicleType.objects.values_list("name", flat=True))


def test_reservation_fk_survives_the_rename():
    from apps.reservations.factories import ReservationFactory

    vt = VehicleTypeFactory(name="Sprinter Van")
    res = ReservationFactory(vehicle=vt)
    res.refresh_from_db()
    assert res.vehicle.name == "Sprinter Van"
