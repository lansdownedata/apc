"""The portal's wedding form: the public one's validation, minus the contact fields."""

import json

import pytest

from apps.leads.factories import VehicleTypeFactory
from apps.leads.forms import PortalWeddingForm
from apps.public.tests.test_wedding_form import _legs, _post

pytestmark = pytest.mark.django_db


def _portal(**over):
    """The public payload minus the fields a lead already owns."""
    data = _post(**over)
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)
    return data


def test_it_validates_without_a_name_or_contact_details():
    form = PortalWeddingForm(_portal())
    assert form.is_valid(), form.errors


def test_it_inherits_the_twelve_leg_ceiling():
    assert not PortalWeddingForm(_portal(legs_json=json.dumps(_legs(13)))).is_valid()


@pytest.mark.parametrize("pax", [0, 500])
def test_it_inherits_the_passenger_bounds(pax):
    legs = _legs()
    legs[0]["pax"] = pax
    assert not PortalWeddingForm(_portal(legs_json=json.dumps(legs))).is_valid()


def test_it_still_re_derives_the_vehicle_recommendation():
    form = PortalWeddingForm(_portal())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["legs"][0]["vehicle"] == "2 × 56-passenger coach"


def test_an_assigned_vehicle_comes_back_keyed_by_leg():
    vehicle = VehicleTypeFactory(name="40-Passenger Coach", capacity=40)
    form = PortalWeddingForm(_portal(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vehicles"] == {"guests-in": vehicle}


def test_an_unknown_vehicle_is_dropped_not_rejected():
    """A vehicle retired mid-edit must not cost the agent the whole day's work."""
    form = PortalWeddingForm(_portal(vehicles_json=json.dumps({"guests-in": 999999})))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vehicles"] == {}


def test_a_retired_vehicle_is_dropped_too():
    vehicle = VehicleTypeFactory(name="Retired Coach", active=False)
    form = PortalWeddingForm(_portal(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vehicles"] == {}


def test_malformed_vehicle_json_is_dropped():
    form = PortalWeddingForm(_portal(vehicles_json="{not json"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vehicles"] == {}


def test_no_vehicles_posted_is_fine():
    form = PortalWeddingForm(_portal())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vehicles"] == {}


def test_the_contact_fields_are_gone_rather_than_optional():
    """The lead owns the contact; there is no honeypot behind auth."""
    for field in ("name", "email", "phone", "company"):
        assert field not in PortalWeddingForm().fields


def test_a_posted_honeypot_is_simply_ignored():
    form = PortalWeddingForm(_portal(company="spam"))
    assert form.is_valid(), form.errors
