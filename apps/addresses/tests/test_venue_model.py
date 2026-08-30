import pytest

from apps.addresses.factories import VenueFactory
from apps.addresses.models import Venue

pytestmark = pytest.mark.django_db


def test_kind_choices():
    assert Venue.Kind.VENUE == "venue"
    assert Venue.Kind.HOTEL == "hotel"
    assert Venue.Kind.CHURCH == "church"


def test_defaults_to_a_reception_venue():
    assert VenueFactory().kind == Venue.Kind.VENUE


def test_str_is_the_name():
    assert str(VenueFactory(name="Rose Hill Manor")) == "Rose Hill Manor"


def test_enrichment_fields_start_empty():
    """Seeds carry names and towns only — LocationIQ fills the rest in later."""
    venue = VenueFactory()
    assert venue.address == ""
    assert venue.latitude is None
    assert venue.longitude is None
    assert venue.locationiq_place_id == ""


def test_vehicle_cap_is_optional():
    assert VenueFactory().vehicle_cap is None
    assert VenueFactory(vehicle_cap=40).vehicle_cap == 40


def test_lead_hits_starts_at_zero():
    assert VenueFactory().lead_hits == 0


def test_location_line_joins_town_and_state():
    venue = VenueFactory(city="Leesburg", state="VA")
    assert venue.location_line == "Leesburg, VA"


def test_location_line_prefers_a_street_address_once_enriched():
    venue = VenueFactory(address="14572 Loyalty Rd", city="Leesburg", state="VA")
    assert venue.location_line == "14572 Loyalty Rd, Leesburg, VA"
