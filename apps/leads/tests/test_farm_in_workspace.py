"""APC-136 — the GNet banner on the quote workspace."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import farm_in
from apps.dispatch.factories import FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import LeadFactory

pytestmark = pytest.mark.django_db


def _page(client, lead) -> str:
    client.force_login(UserFactory())
    resp = client.get(reverse("lead_detail", args=[lead.pk]))
    assert resp.status_code == 200
    return resp.content.decode()


def _gnet_request(tid: str = "tx-ws-1", **fields) -> FarmInRequest:
    farm_in.handle_new(sample_payload(), transaction_id=tid, delivery_id="d")
    if fields:
        FarmInRequest.objects.filter(transaction_id=tid).update(**fields)
    return FarmInRequest.objects.get(transaction_id=tid)


def test_gnet_lead_shows_banner_with_actions_and_hides_book_now(client):
    req = _gnet_request()
    page = _page(client, req.lead)
    assert "data-farm-in-banner" in page
    assert "GNet request" in page
    assert req.company.name in page
    assert reverse("farm_in_accept", args=[req.pk]) in page
    assert reverse("farm_in_decline", args=[req.pk]) in page
    assert reverse("farm_in_request_coverage", args=[req.pk]) in page
    assert reverse("farm_in_send_back", args=[req.pk]) not in page
    assert "Book now" not in page
    assert "Send quote" not in page
    assert "Mark lost" not in page


def test_dispatch_desk_banner_offers_send_back(client):
    req = _gnet_request(tid="tx-ws-2", desk=FarmInRequest.Desk.DISPATCH)
    page = _page(client, req.lead)
    assert reverse("farm_in_send_back", args=[req.pk]) in page
    assert reverse("farm_in_request_coverage", args=[req.pk]) not in page


def test_accepted_gnet_lead_shows_reported_line(client):
    req = _gnet_request(tid="tx-ws-3", status=FarmInRequest.Status.ACCEPTED)
    FarmInStatusPushFactory(request=req, status="CONFIRMED", total_amount="165.00", state="sent")
    page = _page(client, req.lead)
    assert "Reported to GNet" in page
    assert "CONFIRMED" in page
    assert reverse("order_detail", args=[req.lead_id]) in page
    assert reverse("farm_in_accept", args=[req.pk]) not in page


def test_ordinary_lead_has_no_banner(client):
    page = _page(client, LeadFactory())
    assert "data-farm-in-banner" not in page
    assert "Mark lost" in page
