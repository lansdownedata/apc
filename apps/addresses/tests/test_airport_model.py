import pytest

from apps.addresses.factories import AirportFactory
from apps.addresses.models import Airport

pytestmark = pytest.mark.django_db


def test_label_prefixes_the_iata_code():
    airport = AirportFactory(iata="DCA", name="Ronald Reagan Washington National Airport")
    assert airport.label == "DCA — Ronald Reagan Washington National Airport"


def test_label_falls_back_to_name_when_no_iata():
    airport = AirportFactory(iata="", name="Ocean Reef Club Airport")
    assert airport.label == "Ocean Reef Club Airport"


def test_str_is_the_label():
    assert str(AirportFactory(iata="BOS", name="Boston Logan")) == "BOS — Boston Logan"


def test_size_choices():
    assert Airport.Size.LARGE == "large_airport"
    assert Airport.Size.MEDIUM == "medium_airport"


def test_enrichment_fields_default_blank():
    airport = AirportFactory()
    assert airport.locationiq_place_id == ""
    assert airport.line1 == ""
    assert airport.postal == ""
    assert airport.display_name == ""
    assert airport.enriched_at is None
