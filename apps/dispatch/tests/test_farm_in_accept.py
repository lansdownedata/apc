from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.core.choices import Channel
from apps.dispatch import farm_in, farm_in_actions
from apps.dispatch.farm_in_actions import FarmInActionError
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.integrations import gnet
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead, VehicleType
from apps.leads.services import BookLeadError
from apps.payments.models import PaymentPlan
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


@pytest.fixture
def agent():
    return UserFactory()


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    vt.refresh_from_db()
    return vt


@pytest.fixture(autouse=True)
def gnet_key(settings):
    """Accept refuses without a key; on-commit pushes never run here, so no HTTP leaves."""
    settings.GNET_API_KEY = "k"


@pytest.fixture(autouse=True)
def airport_and_airline():
    AirportFactory(iata="LAX", name="Los Angeles International", timezone="America/Los_Angeles")
    AirlineFactory(iata="UA")


def _req(payload=None) -> FarmInRequest:
    farm_in.handle_new(payload or sample_payload(), transaction_id=TID, delivery_id="d-1")
    return FarmInRequest.objects.select_related("company").get(transaction_id=TID)


@pytest.fixture
def req(sedan):
    return _req()


def _accept(req, agent) -> tuple[FarmInRequest, Lead, Reservation]:
    out = farm_in_actions.accept(req.pk, user=agent)
    out.refresh_from_db()
    return out, out.lead, out.reservation


def test_accept_requires_price_and_vehicle(req, agent):
    FarmInRequest.objects.filter(pk=req.pk).update(quoted_amount=None)
    with pytest.raises(FarmInActionError, match="Price the request first") as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 400
    FarmInRequest.objects.filter(pk=req.pk).update(quoted_amount=Decimal("165"), vehicle=None)
    with pytest.raises(FarmInActionError, match="Choose a vehicle first") as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 400


def test_accept_refuses_without_a_gnet_api_key(req, agent, settings):
    settings.GNET_API_KEY = ""
    with pytest.raises(FarmInActionError, match="set GNET_API_KEY") as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 400
    assert Lead.objects.count() == 0 and not FarmInStatusPush.objects.exists()
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.PENDING


def test_accept_books_a_lead_on_the_companys_contact(req, agent):
    contact = req.company.contacts.get()
    _, lead, _ = _accept(req, agent)
    assert lead.channel == Channel.GNET and lead.status == Lead.Status.BOOKED
    assert lead.contact == contact and lead.contact.company == req.company
    assert "Please confirm receipt" in lead.notes
    assert "GNet res no 1715016686" in lead.notes
    assert "Meet at curb" not in lead.notes


def test_accept_without_pickup_time_books_and_alerts(sedan, agent):
    payload = sample_payload()
    del payload["locations"]["pickup"]["time"]
    out, lead, res = _accept(_req(payload), agent)
    assert res.pickup_date is None and res.pickup_time is None
    assert out.status == FarmInRequest.Status.ACCEPTED
    assert lead.status == Lead.Status.BOOKED
    assert out.alert == "No pickup time: set it on the order"


def test_accept_resolves_its_alerts_and_keeps_the_rest(sedan, agent):
    payload = sample_payload()
    del payload["locations"]["pickup"]["time"]
    req = _req(payload)
    FarmInRequest.objects.filter(pk=req.pk).update(
        alert=" · ".join(
            [
                farm_in.ALERT_UPDATED,
                farm_in.ALERT_UNMAPPED.format(code="SEDAN"),
                farm_in.ALERT_NO_PICKUP,
                farm_in.ALERT_TZ_ASSUMED,
            ]
        )
    )
    out, _, _ = _accept(req, agent)
    assert out.alert == " · ".join(
        [farm_in.ALERT_UPDATED, farm_in.ALERT_TZ_ASSUMED, "No pickup time: set it on the order"]
    )


def test_accept_marks_request_and_enqueues_confirmed(
    req, agent, settings, django_capture_on_commit_callbacks
):
    settings.GNET_API_KEY = "k"
    settings.GNET_GATEWAY_URL = "https://gw.test"
    with patch.object(gnet, "requests") as fake:
        resp = MagicMock(status_code=200, text="", content=b"x")
        resp.json.return_value = {"ok": True}
        fake.request.return_value = resp
        with django_capture_on_commit_callbacks(execute=True):
            farm_in_actions.accept(req.pk, user=agent)
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.ACCEPTED
    assert req.lead is not None and req.reservation.lead == req.lead
    assert req.decided_by == agent and req.decided_at is not None
    assert req.alert == ""
    push = FarmInStatusPush.objects.get(request=req)
    assert push.status == "CONFIRMED" and push.total_amount == Decimal("165.00")
    assert push.state == FarmInStatusPush.State.SENT


def test_accept_applies_the_farm_in_clock(req, agent):
    _, lead, _ = _accept(req, agent)
    assert lead.is_farm_in is True
    assert PaymentPlan.objects.filter(lead=lead).exists()


def test_accept_twice_second_gets_409(req, agent):
    farm_in_actions.accept(req.pk, user=agent)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.accept(req.pk, user=agent)
    assert exc.value.status == 409
    assert Lead.objects.count() == 1


def test_accept_rolls_back_order_when_booking_fails(req, agent):
    with patch.object(farm_in_actions, "book_lead", side_effect=BookLeadError("No can do")):
        with pytest.raises(FarmInActionError, match="No can do"):
            farm_in_actions.accept(req.pk, user=agent)
    assert Lead.objects.count() == 0 and Reservation.objects.count() == 0
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.PENDING and req.lead is None
    assert not FarmInStatusPush.objects.exists()
