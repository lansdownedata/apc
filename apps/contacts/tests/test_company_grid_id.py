"""APC-136 — the company page's GNet griddID field."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory
from apps.dispatch import farm_in
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload

pytestmark = pytest.mark.django_db


def _post(client, company, **data):
    client.force_login(UserFactory())
    return client.post(reverse("company_update", args=[company.pk]), data)


def test_company_grid_id_saves_and_blank_is_null(client):
    company = CompanyFactory()
    assert _post(client, company, gnet_grid_id="  GRID-1 ").status_code == 200
    company.refresh_from_db()
    assert company.gnet_grid_id == "GRID-1"
    assert _post(client, company, gnet_grid_id="   ").status_code == 200
    company.refresh_from_db()
    assert company.gnet_grid_id is None


def test_company_grid_id_duplicate_is_a_clear_400(client):
    CompanyFactory(gnet_grid_id="TAKEN")
    company = CompanyFactory()
    resp = _post(client, company, gnet_grid_id="TAKEN")
    assert resp.status_code == 400
    assert resp.json()["error"] == "That griddID belongs to another company."
    taken_name = _post(client, company, name=CompanyFactory().name)
    assert taken_name.json()["error"] == "A company with that name already exists."


def test_company_grid_id_save_reparents_requests(client):
    farm_in.handle_new(sample_payload(), transaction_id="tx-gid", delivery_id="d")
    req = FarmInRequest.objects.get(transaction_id="tx-gid")
    # Arrival filed it under an auto-created company; free the id, then claim it.
    assert _post(client, req.company, gnet_grid_id="").status_code == 200
    target = CompanyFactory()
    assert _post(client, target, gnet_grid_id=req.requester_grid_id).status_code == 200
    req.refresh_from_db()
    assert req.company_id == target.pk


def test_company_page_renders_the_field(client):
    company = CompanyFactory(gnet_grid_id="G-9")
    client.force_login(UserFactory())
    page = client.get(reverse("company_detail", args=[company.pk])).content.decode()
    assert "GNet griddID" in page
    assert "G-9" in page
