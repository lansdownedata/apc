import json
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirportFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation, Stop

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
        "hours": 1,
        "minHours": 0,
        "stops": [{"address": "A"}, {"address": "B"}],
    }
    base.update(over)
    return base


def _post(client, payload):
    return client.post(
        reverse("reservation_save"),
        data=json.dumps(payload),
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
    _post(client, _draft(lead, tripType="hourly", rate=295, hours=0, minHours=5))
    assert lead.reservations.get().line_total == Decimal("1475.00")


def test_save_updates_existing(client):
    res = TransferReservationFactory(service_type=ServiceTypeFactory(name="Old Service"))
    client.force_login(UserFactory())
    new_service = ServiceTypeFactory(name="New Service")
    _post(client, _draft(res.lead, id=res.pk, serviceType=new_service.pk, rate=200))
    res.refresh_from_db()
    assert res.service_type == new_service
    assert res.lead.reservations.count() == 1


def test_editing_booked_lead_keeps_status_booked(client):
    res = TransferReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    client.force_login(UserFactory())
    _post(client, _draft(res.lead, id=res.pk, rate=999))
    res.lead.refresh_from_db()
    assert res.lead.status == Lead.Status.BOOKED


def test_save_rejects_malformed_json(client):
    client.force_login(UserFactory())
    resp = client.post(reverse("reservation_save"), data="{bad", content_type="application/json")
    assert resp.status_code == 400


def test_save_rejects_negative_amount(client):
    lead = LeadFactory()
    client.force_login(UserFactory())
    resp = _post(client, _draft(lead, rate=-1))
    assert resp.status_code == 400


def test_save_requires_login(client):
    lead = LeadFactory()
    resp = _post(client, _draft(lead))
    assert resp.status_code == 302


def test_duplicate_clones_reservation_and_stops(client):
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    res = TransferReservationFactory(service_type=wedding, la_reservation_id="LA-1")
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(reservation=res, sequence=0, address="A")
    Stop.objects.create(reservation=res, sequence=1, address="B", note="wait")
    client.force_login(UserFactory())
    resp = client.post(reverse("reservation_duplicate", args=[res.pk]))
    assert resp.status_code == 302
    clone = res.lead.reservations.exclude(pk=res.pk).get()
    assert clone.service_type == wedding  # the copy keeps the same catalog entry
    assert clone.la_reservation_id == ""
    assert [s.address for s in clone.ordered_stops] == ["A", "B"]
    assert clone.ordered_stops[1].note == "wait"


def test_duplicate_carries_coordinates_airport_and_timezone(client):
    lax = AirportFactory(iata="LAX", timezone="America/Los_Angeles")
    res = TransferReservationFactory(pickup_timezone="America/Los_Angeles")
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="LAX",
        latitude=Decimal("33.941600"),
        longitude=Decimal("-118.408500"),
        airport=lax,
        note="curb",
    )
    Stop.objects.create(reservation=res, sequence=1, address="Hotel")
    client.force_login(UserFactory())
    client.post(reverse("reservation_duplicate", args=[res.pk]))
    clone = res.lead.reservations.exclude(pk=res.pk).get()
    pickup = clone.stops.order_by("sequence").first()
    assert pickup.latitude == Decimal("33.941600")
    assert pickup.longitude == Decimal("-118.408500")
    assert pickup.airport_id == lax.pk
    assert clone.pickup_timezone == "America/Los_Angeles"


def test_delete_removes_reservation_and_stops(client):
    res = TransferReservationFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("reservation_delete", args=[res.pk]))
    assert resp.status_code == 302
    assert not Reservation.objects.filter(pk=res.pk).exists()
    assert not Stop.objects.filter(reservation_id=res.pk).exists()


def test_delete_withdraws_affiliate_coverage_first(client, monkeypatch):
    """A deleted trip must not leave an affiliate holding coverage nobody can withdraw —
    no screen lists assignments by vendor. The Assignment row goes with the trip (CASCADE),
    so the withdrawal is observed at the service door it has to pass through."""
    from apps.dispatch import services as dispatch_services
    from apps.dispatch.models import Assignment
    from apps.vendors.factories import VendorFactory

    res = TransferReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    dispatch_services.send_offer(res, VendorFactory(), payout=Decimal("100.00"))
    released = []
    real = dispatch_services.release_trips

    def spy(reservations, *, note):
        withdrawn = real(reservations, note=note)
        released.extend((a.status, a.note) for a in withdrawn)
        return withdrawn

    monkeypatch.setattr(dispatch_services, "release_trips", spy)
    client.force_login(UserFactory())
    client.post(reverse("reservation_delete", args=[res.pk]))

    assert released == [(Assignment.Status.WITHDRAWN, "Trip removed")]
    assert not Assignment.objects.filter(reservation_id=res.pk).exists()


def test_duplicate_requires_login(client):
    res = TransferReservationFactory()
    resp = client.post(reverse("reservation_duplicate", args=[res.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url
