"""Edits after LA push raise an alert; resend endpoint re-pushes (gated)."""

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.notifications.models import Notification
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _draft(lead, **over):
    base = {
        "lead_id": lead.pk,
        "tripType": "transfer",
        "service": "Transfer",
        "date": "2026-07-04",
        "time": "15:00",
        "vehicle": "",
        "pax": 2,
        "baseRate": 200,
        "hours": 0,
        "hourlyRate": 0,
        "minHours": 0,
        "stops": [{"address": "A"}, {"address": "B"}],
    }
    base.update(over)
    return base


def _post_save(client, payload):
    return client.post(
        reverse("reservation_save"),
        data=json.dumps(payload),
        content_type="application/json",
    )


def test_editing_pushed_reservation_raises_la_changed_alert(client):
    res = TransferReservationFactory(la_reservation_id="67890")
    client.force_login(UserFactory())
    _post_save(client, _draft(res.lead, id=res.pk, baseRate=250))
    assert Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_editing_unpushed_reservation_raises_no_alert(client):
    res = TransferReservationFactory(la_reservation_id="")
    client.force_login(UserFactory())
    _post_save(client, _draft(res.lead, id=res.pk, baseRate=250))
    assert not Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_creating_reservation_raises_no_alert(client):
    lead = LeadFactory()
    client.force_login(UserFactory())
    _post_save(client, _draft(lead))
    assert not Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_deleting_pushed_reservation_raises_alert(client):
    res = TransferReservationFactory(la_reservation_id="67890")
    client.force_login(UserFactory())
    client.post(reverse("reservation_delete", args=[res.pk]))
    assert Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_deleting_unpushed_reservation_raises_no_alert(client):
    res = TransferReservationFactory(la_reservation_id="")
    client.force_login(UserFactory())
    client.post(reverse("reservation_delete", args=[res.pk]))
    assert not Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_resend_requires_payment_access(client):
    lead = LeadFactory()
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    resp = client.post(reverse("lead_resend_la", args=[lead.pk]))
    assert resp.status_code == 403


def test_resend_requires_login(client):
    lead = LeadFactory()
    resp = client.post(reverse("lead_resend_la", args=[lead.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_resend_pushes_lead(client):
    lead = LeadFactory()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch("apps.leads.views.la_sync.push_lead_bookings") as push:
        resp = client.post(reverse("lead_resend_la", args=[lead.pk]))
    push.assert_called_once()
    called_lead = push.call_args[0][0]
    assert called_lead.pk == lead.pk
    assert resp.status_code in (200, 302)
