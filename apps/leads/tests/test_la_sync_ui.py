"""Edits after an LA push raise an alert; no LimoAnywhere state shows in the UI."""

import json

import pytest
from django.conf import settings
from django.urls import NoReverseMatch, reverse

from apps.accounts.factories import UserFactory
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


@pytest.mark.parametrize(
    "result", [ZapEvent.Result.SUCCESS, ZapEvent.Result.ERROR, ZapEvent.Result.PREVIEW]
)
def test_booked_order_shows_no_limoanywhere_state(logged_in_client, result):
    """LimoAnywhere is out (2026-11-20): old sync rows must not surface on the order."""
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=result,
        payload={"booking": {}},
    )
    resp = logged_in_client.get(reverse("lead_detail", args=[res.lead.pk]))
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "LimoAnywhere" not in html
    assert "LA · " not in html
    assert "Resend to LA" not in html


def test_resend_to_la_endpoint_is_gone():
    with pytest.raises(NoReverseMatch):
        reverse("lead_resend_la", args=[1])


def test_app_js_never_mentions_limoanywhere():
    """Modals built in app.js (payload preview, trip status confirm) are staff-visible copy."""
    js = (settings.BASE_DIR / "static" / "js" / "app.js").read_text()
    assert "LimoAnywhere" not in js
    assert "showLaPayload" not in js
