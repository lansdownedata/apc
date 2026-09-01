"""VenueAdmin — the office surface for maintaining venue vehicle-access limits (APC-9).

`vehicle_cap` is editable straight from the changelist; a "cap on file" filter lets the
office work the gaps; the access fields are grouped on the edit form.
"""

import pytest
from django.contrib.admin.sites import site
from django.urls import reverse

from apps.addresses.admin import VenueAdmin
from apps.addresses.factories import VenueFactory
from apps.addresses.models import Venue

pytestmark = pytest.mark.django_db

CHANGELIST_URL = "admin:addresses_venue_changelist"


@pytest.fixture(autouse=True)
def _empty_venue_table():
    """Migration 0009 seeds the directory; the filter tests assert on exact rows."""
    Venue.objects.all().delete()


def test_vehicle_cap_is_editable_from_the_changelist():
    assert "vehicle_cap" in VenueAdmin.list_editable
    assert "vehicle_cap" in VenueAdmin.list_display
    # list_editable requires the field not be the first (linked) column.
    assert VenueAdmin.list_display[0] != "vehicle_cap"


def test_access_fields_are_grouped_on_the_edit_form():
    groups = {name: opts for name, opts in VenueAdmin.fieldsets}
    access = next(opts["fields"] for name, opts in VenueAdmin.fieldsets if name == "Vehicle access")
    assert set(access) == {"vehicle_cap", "cap_note", "access_note"}
    assert "Location" in groups


def test_search_still_covers_name_and_town():
    assert "name" in VenueAdmin.search_fields
    assert "city" in VenueAdmin.search_fields


def test_has_cap_filter_partitions_the_directory(admin_client):
    with_cap = VenueFactory(name="Capped Venue", vehicle_cap=40)
    without_cap = VenueFactory(name="Uncapped Venue", vehicle_cap=None)

    base = reverse(CHANGELIST_URL)
    yes = admin_client.get(base, {"has_cap": "yes"})
    no = admin_client.get(base, {"has_cap": "no"})

    assert list(yes.context["cl"].queryset) == [with_cap]
    assert list(no.context["cl"].queryset) == [without_cap]


def test_changelist_renders(admin_client):
    VenueFactory.create_batch(3)
    resp = admin_client.get(reverse(CHANGELIST_URL))
    assert resp.status_code == 200


def test_editing_a_cap_from_the_changelist_persists(admin_client):
    venue = VenueFactory(name="Editable Venue", vehicle_cap=None)
    model_admin = VenueAdmin(Venue, site)

    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "1",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "form-0-id": str(venue.pk),
        "form-0-vehicle_cap": "38",
        "_save": "Save",
    }
    resp = admin_client.post(reverse(CHANGELIST_URL), data)

    assert resp.status_code == 302
    venue.refresh_from_db()
    assert venue.vehicle_cap == 38
    # sanity: the admin still knows this row has no cap-on-file until we set one
    assert model_admin.has_cap(venue) is True
