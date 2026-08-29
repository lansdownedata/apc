"""Edits after LA push raise an alert; resend endpoint re-pushes (gated)."""

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.integrations.models import ZapEvent
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory, TransferReservationFactory

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
        "rate": 200,
        "hours": 0,
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
    _post_save(client, _draft(res.lead, id=res.pk, rate=250))
    assert Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_editing_unpushed_reservation_raises_no_alert(client):
    res = TransferReservationFactory(la_reservation_id="")
    client.force_login(UserFactory())
    _post_save(client, _draft(res.lead, id=res.pk, rate=250))
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


def test_detail_shows_preview_badge_and_payload_script(logged_in_client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=ZapEvent.Result.PREVIEW,
        payload={"booking": {"search_result_id": None}},
    )
    resp = logged_in_client.get(reverse("lead_detail", args=[res.lead.pk]))
    html = resp.content.decode()
    assert "LimoAnywhere sync" in html
    assert "Preview" in html
    assert f'id="la-payload-{res.pk}"' in html  # json_script for the modal


def test_detail_shows_sent_confirmation(logged_in_client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), la_confirmation="ABC123")
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=ZapEvent.Result.SUCCESS,
    )
    resp = logged_in_client.get(reverse("lead_detail", args=[res.lead.pk]))
    assert "ABC123" in resp.content.decode()


def test_resend_button_hidden_for_preview_when_unconfigured(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=ZapEvent.Result.PREVIEW,
    )
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.get(reverse("lead_detail", args=[res.lead.pk]))
    assert resp.context["can_resend_la"] is False
    assert "Resend to LA" not in resp.content.decode()


def test_resend_button_shown_for_preview_once_configured(client, settings):
    settings.LA_CLIENT_ID = "cid"
    settings.LA_CLIENT_SECRET = "cs"
    settings.LA_COMPANY_ALIAS = "allpro"
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=ZapEvent.Result.PREVIEW,
    )
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.get(reverse("lead_detail", args=[res.lead.pk]))
    assert resp.context["can_resend_la"] is True
    assert "Resend to LA" in resp.content.decode()
