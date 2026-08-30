import pytest

from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def test_a_hand_added_reservation_has_no_source_leg():
    """Blank is what keeps the wedding builder from touching a trip an agent typed."""
    assert ReservationFactory().source_leg_id == ""


def test_a_generated_reservation_remembers_its_leg():
    res = ReservationFactory(source_leg_id="guests-in")
    assert res.source_leg_id == "guests-in"


def test_source_leg_is_indexed_for_lookup():
    field = ReservationFactory()._meta.get_field("source_leg_id")
    assert field.db_index is True
    assert field.max_length == 40
