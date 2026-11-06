import pytest
from django.db import IntegrityError

from apps.contacts.factories import CompanyFactory
from apps.core.choices import Channel

pytestmark = pytest.mark.django_db


def test_channel_has_gnet():
    assert Channel.GNET == "gnet"
    assert Channel.GNET.label == "GNet"


def test_blank_grid_id_saves_as_null():
    c = CompanyFactory(gnet_grid_id="")
    c.refresh_from_db()
    assert c.gnet_grid_id is None


def test_two_companies_without_grid_id_coexist():
    CompanyFactory(gnet_grid_id="")
    CompanyFactory(gnet_grid_id=None)  # no IntegrityError


def test_grid_id_is_unique():
    CompanyFactory(gnet_grid_id="acmelimo")
    with pytest.raises(IntegrityError):
        CompanyFactory(gnet_grid_id="acmelimo")


def test_grid_id_is_stripped():
    c = CompanyFactory(gnet_grid_id="  acmelimo ")
    c.refresh_from_db()
    assert c.gnet_grid_id == "acmelimo"
