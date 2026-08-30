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


def test_seeded_airports_carry_their_timezone(db):
    from apps.addresses.models import Airport

    assert Airport.objects.get(iata="IAD").timezone == "America/New_York"
    assert Airport.objects.get(iata="LAX").timezone == "America/Los_Angeles"
    assert Airport.objects.get(iata="DEN").timezone == "America/Denver"
    assert not Airport.objects.filter(timezone="").exists()


def test_airport_factory_defaults_to_eastern(db):
    from apps.addresses.factories import AirportFactory

    assert AirportFactory().timezone == "America/New_York"


# --- ground-transport eligibility vs. scheduled-service flags (2026-08-29) -----------


def test_seeded_airports_keep_the_curated_863_ground_transport_eligible(db):
    """The migration reload must never flip an existing curated row's eligibility, even
    though it also inserts thousands of new global rows flagged the opposite way."""
    # 863 curated + 11 US territories
    assert Airport.objects.filter(serves_ground_transport=True).count() == 874


def test_seeded_airports_have_an_unambiguous_scheduled_service_flag(db):
    assert Airport.objects.filter(has_scheduled_service=True).count() == 3246
    assert not Airport.objects.exclude(has_scheduled_service__in=(True, False)).exists()


def test_a_military_field_serves_ground_transport_without_scheduled_service(db):
    """Andrews is a legitimate pickup (the client's own service area) but has no
    passenger flights to look up — the two flags are independent, not one axis."""
    adw = Airport.objects.get(iata="ADW")
    assert adw.serves_ground_transport is True
    assert adw.has_scheduled_service is False


def test_a_foreign_airport_is_not_ground_transport_eligible(db):
    """Heathrow exists only so a flight's other endpoint has a resolvable name — it must
    never be offered as a pickup/drop-off."""
    lhr = Airport.objects.get(iata="LHR")
    assert lhr.serves_ground_transport is False
    assert lhr.has_scheduled_service is True


def test_a_us_territory_is_ground_transport_eligible(db):
    """San Juan is a real customer destination — customers fly there and a car may be
    sent (spec: SJU was the #2 origin in a live BWI/JFK arrivals sample)."""
    sju = Airport.objects.get(iata="SJU")
    assert sju.serves_ground_transport is True
    assert sju.has_scheduled_service is True
