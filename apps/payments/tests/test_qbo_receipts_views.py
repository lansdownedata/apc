"""APC-108 — Retry for one card receipt (and its customer first)."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.billing.models import CustomerLink, SyncState
from apps.billing.tests.test_qbo_receipts import (  # noqa: F401
    REQ,
    _card_charge,
    _customer,
    _fault,
    _receipt,
    ready,
    realm,
    receipt_mapping,
)
from apps.integrations import qbo
from apps.payments.factories import ChargeFactory
from apps.payments.models import Charge

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ready")]


@pytest.fixture
def accountant(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def _url(charge) -> str:
    return reverse("charge_qbo_sync", args=[charge.pk])


def _without_customer(charge) -> None:
    CustomerLink.objects.filter(contact=charge.reservation.lead.contact).delete()


def test_charge_sync_pushes_customer_then_receipt(accountant) -> None:
    charge = _card_charge()
    _without_customer(charge)
    contact = charge.reservation.lead.contact
    answers = [{"Customer": _customer(id_="61", name=contact.name)}, _receipt()]
    with patch(REQ, side_effect=answers) as req:
        resp = accountant.post(_url(charge))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "state": "synced", "error": ""}
    assert [c.args[1] for c in req.call_args_list] == ["customer", "salesreceipt"]
    charge.refresh_from_db()
    assert charge.qbo_sync_state == SyncState.SYNCED


def test_charge_sync_waits_when_customer_refused(accountant) -> None:
    charge = _card_charge()
    _without_customer(charge)
    refusal = _fault("6000", "A business validation error has occurred", detail="Bad email")
    with patch(REQ, side_effect=[refusal]) as req:
        resp = accountant.post(_url(charge))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["state"] == "error" and body["error"]
    assert [c.args[1] for c in req.call_args_list] == ["customer"]
    charge.refresh_from_db()
    assert charge.qbo_sync_state == SyncState.NOT_SYNCED


def test_charge_sync_gated(client) -> None:
    charge = _card_charge()
    client.force_login(UserFactory())
    assert client.post(_url(charge)).status_code == 403
    client.logout()
    resp = client.post(_url(charge))
    assert resp.status_code == 302 and "login" in resp["Location"]


def test_charge_sync_400_disconnected(accountant) -> None:
    charge = _card_charge()
    with patch("apps.integrations.qbo.is_connected", return_value=False):
        assert accountant.post(_url(charge)).status_code == 400


def test_charge_sync_502_outage(accountant) -> None:
    charge = _card_charge()
    with patch(REQ, side_effect=qbo.QboAPIError(503, "down")):
        resp = accountant.post(_url(charge))
    assert resp.status_code == 502
    assert resp.json()["error"] == "QuickBooks couldn't be reached. Try again in a minute."


def test_charge_sync_404_for_deposit(accountant) -> None:
    charge = _card_charge()
    deposit = ChargeFactory(kind=Charge.Kind.DEPOSIT, reservation=charge.reservation)
    assert accountant.post(_url(deposit)).status_code == 404
    no_trip = ChargeFactory(kind=Charge.Kind.TRIP)
    assert accountant.post(_url(no_trip)).status_code == 404
