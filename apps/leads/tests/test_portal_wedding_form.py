"""The portal's wedding form: the public one's validation, minus the contact fields."""

import pytest

from apps.leads.forms import PortalWeddingForm
from apps.public.tests.test_wedding_form import _post

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


def test_the_contact_fields_are_gone_rather_than_optional():
    """The lead owns the contact; there is no honeypot behind auth."""
    for field in ("name", "email", "phone", "company"):
        assert field not in PortalWeddingForm().fields


def test_a_posted_honeypot_is_simply_ignored():
    form = PortalWeddingForm(_portal(company="spam"))
    assert form.is_valid(), form.errors


# --- per-leg trip type + hours (the office may bill a leg by the hour) --------------
