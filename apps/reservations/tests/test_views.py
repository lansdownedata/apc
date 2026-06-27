import json
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _draft(lead, **over):
    base = {
        "lead_id": lead.pk, "tripType": "transfer", "service": "Transfer",
        "date": "2026-07-04", "time": "15:00", "vehicle": "", "pax": 2,
        "baseRate": 200, "hours": 0, "hourlyRate": 0, "minHours": 0,
        "stops": [{"address": "A"}, {"address": "B"}],
    }
    base.update(over)
    return base


def _post(client, payload):
    return client.post(
        reverse("reservation_save"), data=json.dumps(payload),
        content_type="application/json",
    )


def test_save_creates_transfer(client):
    lead = LeadFactory()
    client.force_login(UserFactory())
    resp = _post(client, _draft(lead))
    assert resp.status_code == 302
    res = lead.reservations.get()
    assert res.line_total == Decimal("200.00")
    assert res.stops.count() == 2


def test_save_creates_hourly_with_minimum(client):
    lead = LeadFactory()
    client.force_login(UserFactory())
    _post(client, _draft(lead, tripType="hourly", baseRate=0, hours=3,
                         hourlyRate=295, minHours=5))
    assert lead.reservations.get().line_total == Decimal("1475.00")


def test_save_updates_existing(client):
    res = TransferReservationFactory(service="Old")
    client.force_login(UserFactory())
    _post(client, _draft(res.lead, id=res.pk, service="New", baseRate=200))
    res.refresh_from_db()
    assert res.service == "New"
    assert res.lead.reservations.count() == 1


def test_editing_booked_lead_keeps_status_booked(client):
    res = TransferReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    client.force_login(UserFactory())
    _post(client, _draft(res.lead, id=res.pk, baseRate=999))
    res.lead.refresh_from_db()
    assert res.lead.status == Lead.Status.BOOKED


def test_save_rejects_malformed_json(client):
    client.force_login(UserFactory())
    resp = client.post(reverse("reservation_save"), data="{bad", content_type="application/json")
    assert resp.status_code == 400


def test_save_rejects_negative_amount(client):
    lead = LeadFactory()
    client.force_login(UserFactory())
    resp = _post(client, _draft(lead, baseRate=-1))
    assert resp.status_code == 400


def test_save_requires_login(client):
    lead = LeadFactory()
    resp = _post(client, _draft(lead))
    assert resp.status_code == 302
