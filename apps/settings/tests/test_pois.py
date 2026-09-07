"""Points of interest — the curated venue/hotel directory, editable in Settings.

The directory already existed (`addresses.Venue`, seeded and admin-only). What is new is
that the office can manage it themselves, and that a POI's limit is expressed as a
*vehicle* ("nothing bigger than a Minibus gets down our drive") rather than a bare number.
Everything sizing a wedding run reads that limit.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import VenueFactory
from apps.addresses.models import Venue
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client


@pytest.fixture
def fleet():
    VehicleType.objects.all().delete()
    return {
        "van": VehicleTypeFactory(name="Sprinter Van", capacity=14, sort_order=0),
        "mini": VehicleTypeFactory(name="Minibus", capacity=20, sort_order=1),
        "coach": VehicleTypeFactory(name="Motor Coach", capacity=56, sort_order=2),
    }


# --- access -------------------------------------------------------------------------


def test_the_list_is_owner_admin_only(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("poi_list")).status_code == 403


def test_an_owner_admin_sees_the_directory(owner):
    VenueFactory(name="The Oak Barn at Loyalty")
    response = owner.get(reverse("poi_list"))
    assert response.status_code == 200
    assert b"The Oak Barn at Loyalty" in response.content


# --- the max-vehicle limit ----------------------------------------------------------


def test_a_poi_defaults_to_no_limit_which_means_our_largest_coach(fleet):
    """ "Default to coach" is stored as *no limit*, not as a pin to today's biggest bus.

    Pinning the FK would leave every POI pointing at a retired vehicle the day the
    catalog changes; deriving it means the fleet stays the single source of truth.
    """
    venue = VenueFactory(name="Somewhere New")
    assert venue.max_vehicle is None
    assert venue.max_passengers == 56


def test_choosing_a_smaller_vehicle_caps_the_run(owner, fleet):
    venue = VenueFactory(name="Narrow Lane Estate")
    owner.post(
        reverse("poi_edit", args=[venue.pk]),
        {
            "name": "Narrow Lane Estate",
            "kind": Venue.Kind.VENUE,
            "address": "",
            "city": "Leesburg",
            "state": "VA",
            "max_vehicle": fleet["mini"].pk,
            "cap_note": "gravel drive, tight turning circle",
            "access_note": "",
            "is_active": "on",
        },
    )
    venue.refresh_from_db()
    assert venue.max_vehicle == fleet["mini"]
    assert venue.max_passengers == 20


def test_a_legacy_numeric_cap_still_applies_until_a_vehicle_is_picked(fleet):
    """`vehicle_cap` is the seeded/CSV column and is not migrated or rewritten.

    It keeps sizing runs exactly as it does today; picking a vehicle in Settings is what
    supersedes it.
    """
    venue = VenueFactory(name="Capped by Contract", vehicle_cap=40)
    assert venue.max_vehicle is None
    assert venue.max_passengers == 40


def test_the_chosen_vehicle_wins_over_the_legacy_number(fleet):
    venue = VenueFactory(name="Both", vehicle_cap=40, max_vehicle=fleet["mini"])
    assert venue.max_passengers == 20


def test_the_picker_offers_only_group_transport_vehicles(owner, fleet):
    """A limo is not a shuttle, so it is not a meaningful ceiling either."""
    VehicleTypeFactory(name="Stretch Limousine", capacity=10, group_transport=False)
    venue = VenueFactory()
    body = owner.get(reverse("poi_edit", args=[venue.pk])).content.decode()
    assert "Motor Coach" in body
    assert "Stretch Limousine" not in body


# --- CRUD ---------------------------------------------------------------------------


def test_an_owner_admin_can_add_a_poi(owner, fleet):
    # A name deliberately absent from the 233 venues seeded by addresses/0004 — a seeded
    # one would pass this even with the POST deleted, and `.get()` would find two.
    owner.post(
        reverse("poi_create"),
        {
            "name": "Nowhere Farm Barn",
            "kind": Venue.Kind.VENUE,
            "address": "",
            "city": "Leesburg",
            "state": "VA",
            "max_vehicle": "",
            "cap_note": "",
            "access_note": "",
            "is_active": "on",
        },
    )
    venue = Venue.objects.get(name="Nowhere Farm Barn")
    assert venue.max_passengers == 56


def test_deleting_a_poi_deactivates_it_rather_than_losing_the_history(owner):
    """The directory feeds typeaheads on quotes already sent — a hard delete would
    silently change what those pages say."""
    venue = VenueFactory(name="Closed Down Barn")
    owner.post(reverse("poi_delete", args=[venue.pk]))
    venue.refresh_from_db()
    assert venue.is_active is False


def test_the_settings_index_links_the_directory(owner):
    body = owner.get(reverse("settings_index")).content.decode()
    assert reverse("poi_list") in body
