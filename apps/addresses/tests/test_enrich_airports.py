from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.addresses.factories import AirportFactory
from apps.addresses.models import Airport

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _empty_airport_table():
    """Migration 0003 seeds 863 real airports; these tests count API calls."""
    Airport.objects.all().delete()


# A LocationIQ /v1/search hit on Logan, ~0 mi from the airport's own coordinates.
NEARBY = [
    {
        "place_id": "abc123",
        "lat": "42.36197",
        "lon": "-71.0079",
        "class": "aeroway",
        "type": "aerodrome",
        "display_name": "Logan International Airport, East Boston, MA, USA",
        "address": {"house_number": "1", "road": "Harborside Drive", "postcode": "02128"},
    }
]
FAR_AWAY = [{**NEARBY[0], "lat": "40.6413", "lon": "-73.7781", "place_id": "wrong"}]


@pytest.fixture
def bos():
    return AirportFactory(
        ident="KBOS",
        iata="BOS",
        name="Boston Logan International Airport",
        city="Boston",
        state="MA",
        latitude=Decimal("42.361970"),
        longitude=Decimal("-71.007900"),
    )


def _run(payload, **kwargs):
    response = mock.Mock(status_code=200)
    response.json.return_value = payload
    target = "apps.addresses.management.commands.enrich_airports"
    with mock.patch(f"{target}.requests.get", return_value=response) as get:
        with mock.patch(f"{target}.time.sleep"):
            call_command("enrich_airports", **kwargs)
    return get


def test_writes_enrichment_fields(bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    _run(NEARBY)
    bos.refresh_from_db()
    assert bos.locationiq_place_id == "abc123"
    assert bos.line1 == "1 Harborside Drive"
    assert bos.postal == "02128"
    assert bos.display_name == "Logan International Airport, East Boston, MA, USA"
    assert bos.enriched_at is not None


def test_rejects_a_result_more_than_three_miles_away(bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    _run(FAR_AWAY)
    bos.refresh_from_db()
    assert bos.locationiq_place_id == ""
    assert bos.line1 == ""
    # A miss still stamps enriched_at so reruns don't retry it forever.
    assert bos.enriched_at is not None


def test_never_overwrites_coordinates(bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    _run(NEARBY)
    bos.refresh_from_db()
    assert bos.latitude == Decimal("42.361970")
    assert bos.longitude == Decimal("-71.007900")


def test_skips_already_enriched_airports(bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    Airport.objects.filter(pk=bos.pk).update(enriched_at=timezone.now())
    get = _run(NEARBY)
    get.assert_not_called()


def test_force_reprocesses_enriched_airports(bos, settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    Airport.objects.filter(pk=bos.pk).update(enriched_at=timezone.now())
    get = _run(NEARBY, force=True)
    assert get.call_count == 1


def test_limit_caps_the_number_processed(settings):
    settings.LOCATIONIQ_API_KEY = "test-key"
    for _ in range(3):
        AirportFactory()
    get = _run(NEARBY, limit=2)
    assert get.call_count == 2


def test_requires_an_api_key(bos, settings):
    settings.LOCATIONIQ_API_KEY = ""
    with pytest.raises(CommandError):
        call_command("enrich_airports")
