"""APC-136 — a GNet request's trip is not removed from the portal: decline the request."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import farm_in
from apps.dispatch import services as dispatch_services
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.reservations import groups
from apps.reservations.drafts import DraftError
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

REFUSAL = "Decline the GNet request instead of removing the trip"


@pytest.fixture
def req() -> FarmInRequest:
    farm_in.handle_new(sample_payload(), transaction_id="tx-guard", delivery_id="d")
    return FarmInRequest.objects.get(transaction_id="tx-guard")


def test_delete_refuses_a_gnet_trip_and_keeps_its_coverage(client, req, monkeypatch):
    released = []
    monkeypatch.setattr(dispatch_services, "release_trips", lambda *a, **k: released.append(a))
    client.force_login(UserFactory())

    resp = client.post(reverse("reservation_delete", args=[req.reservation_id]))

    assert resp.status_code == 302
    assert resp.url == reverse("lead_detail", args=[req.lead_id])
    assert [str(m) for m in get_messages(resp.wsgi_request)] == [REFUSAL]
    assert Reservation.objects.filter(pk=req.reservation_id).exists()
    assert released == []


def test_group_delete_refuses_a_gnet_trip(client, req):
    client.force_login(UserFactory())
    resp = client.post(reverse("reservation_group_delete", args=[req.reservation_id]))
    assert resp.status_code == 302
    assert [str(m) for m in get_messages(resp.wsgi_request)] == [REFUSAL]
    assert Reservation.objects.filter(pk=req.reservation_id).exists()


def test_delete_members_refuses_a_gnet_trip(req, monkeypatch):
    released = []
    monkeypatch.setattr(dispatch_services, "release_trips", lambda *a, **k: released.append(a))
    with pytest.raises(DraftError, match=REFUSAL):
        groups._delete_members([req.reservation])
    assert Reservation.objects.filter(pk=req.reservation_id).exists()
    assert released == []
