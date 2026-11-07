"""APC-136 — the GNet banner on the quote workspace."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import farm_in
from apps.dispatch.factories import FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead

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


def _set_lead(req: FarmInRequest, status: str) -> None:
    Lead.objects.filter(pk=req.lead_id).update(status=status)


def test_header_count_stays_the_context_processors_integer(client):
    """The banner's flag must not shadow the header icon's `farm_in_pending` count."""
    req = _gnet_request(tid="tx-ws-hdr")
    page = _page(client, req.lead)
    assert 'x-data="{n: 1}"' in page
    assert "{n: True}" not in page and "{n: False}" not in page


def test_gnet_lead_cannot_grow_trips_gnet_never_hears_about(client):
    req = _gnet_request(tid="tx-ws-grow")
    page = _page(client, req.lead)
    assert "$dispatch('reservation-new')" not in page
    assert "$dispatch('copy-dates-open'" not in page
    assert 'title="Duplicate reservation"' not in page
    assert 'x-model.number="draft.quantity"' not in page
    assert reverse("reservation_delete", args=[req.reservation_id]) not in page


def test_ordinary_lead_keeps_its_growth_controls(client, logged_in_client):
    from apps.reservations.factories import ReservationFactory

    res = ReservationFactory()
    resp = logged_in_client.get(reverse("lead_detail", args=[res.lead_id]))
    page = resp.content.decode()
    assert "$dispatch('reservation-new')" in page
    assert "$dispatch('copy-dates-open'" in page
    assert 'x-model.number="draft.quantity"' in page
    assert reverse("reservation_delete", args=[res.pk]) in page


@pytest.mark.parametrize(
    ("envelope", "lead_status"),
    [
        (FarmInRequest.Status.ACCEPTED, Lead.Status.BOOKED),
        (FarmInRequest.Status.DECLINED, Lead.Status.LOST),
    ],
)
def test_decided_gnet_lead_offers_no_quote_booking_or_reopen(client, envelope, lead_status):
    req = _gnet_request(tid=f"tx-ws-{envelope}", status=envelope)
    _set_lead(req, lead_status)
    page = _page(client, req.lead)
    assert "Send quote" not in page
    assert "Resend quote" not in page
    assert "Book now" not in page
    assert reverse("lead_reopen", args=[req.lead_id]) not in page


def test_mark_lost_is_refused_while_the_request_is_pending(logged_in_client):
    req = _gnet_request(tid="tx-ws-lost")
    resp = logged_in_client.post(reverse("lead_mark_lost", args=[req.lead_id]), {"reason": "x"})
    assert resp.status_code == 400
    assert "Use Decline on the GNet request" in resp.content.decode()
    req.lead.refresh_from_db()
    assert req.lead.status == Lead.Status.NEW


def test_mark_lost_still_works_once_the_request_is_decided(logged_in_client):
    req = _gnet_request(tid="tx-ws-lost2", status=FarmInRequest.Status.ACCEPTED)
    resp = logged_in_client.post(reverse("lead_mark_lost", args=[req.lead_id]), {"reason": "x"})
    assert resp.status_code == 302


def test_pending_request_on_a_lost_lead_says_so_and_cannot_be_accepted(client):
    req = _gnet_request(tid="tx-ws-lostpending")
    _set_lead(req, Lead.Status.LOST)
    page = _page(client, req.lead)
    banner = page[page.index("data-farm-in-banner") : page.index("<!-- header -->")]
    assert "Lead lost" in banner
    assert 'disabled title="Lead is lost"' in banner


def test_new_affiliate_link_when_the_company_was_created(client):
    req = _gnet_request(tid="tx-ws-co", company_created=True)
    page = _page(client, req.lead)
    assert "New affiliate, review the profile →" in page
    assert reverse("company_detail", args=[req.company_id]) in page


def test_no_new_affiliate_link_for_a_known_company(client):
    req = _gnet_request(tid="tx-ws-co2", company_created=False)
    assert "New affiliate, review the profile" not in _page(client, req.lead)


def test_declined_lead_shows_the_closed_reason(client):
    req = _gnet_request(
        tid="tx-ws-dec", status=FarmInRequest.Status.DECLINED, decline_reason="No cars that day"
    )
    _set_lead(req, Lead.Status.LOST)
    page = _page(client, req.lead)
    banner = page[page.index("data-farm-in-banner") : page.index("<!-- header -->")]
    assert "No cars that day" in banner


def test_unpriced_pending_request_disables_accept(client):
    from apps.reservations.models import Reservation

    req = _gnet_request(tid="tx-ws-unpriced")
    Reservation.objects.filter(pk=req.reservation_id).update(vehicle=None)
    page = _page(client, req.lead)
    assert 'disabled title="Price the trip first"' in page
