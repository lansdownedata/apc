"""The shared address cells (APC-141, spec §5.4)."""

from decimal import Decimal

import pytest

from apps.addresses.factories import AddressFactory
from apps.imports.address import write_address
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "location",
    [
        {"latitude": Decimal("42.350000")},
        {"longitude": Decimal("-71.050000")},
        {"locationiq_place_id": "123"},
        {"display_name": "1 Old St, Boston"},
    ],
)
def test_changed_text_clears_location_when_any_location_field_is_set(location):
    address = AddressFactory(line1="1 Old St", city="Boston", **location)
    vendor = VendorFactory(address=address)
    write_address(vendor, "address", {"Street": "2 New St"})
    address.refresh_from_db()
    assert address.line1 == "2 New St"
    assert (address.latitude, address.longitude) == (None, None)
    assert (address.locationiq_place_id, address.display_name) == ("", "")


def test_unchanged_text_keeps_location():
    address = AddressFactory(line1="1 Old St", locationiq_place_id="123", display_name="Old")
    vendor = VendorFactory(address=address)
    write_address(vendor, "address", {"Street": "1 Old St"})
    address.refresh_from_db()
    assert (address.locationiq_place_id, address.display_name) == ("123", "Old")
