import hashlib
import hmac
import json
import threading
from datetime import UTC, date, datetime, time
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.urls import reverse

from apps.contacts.models import Company
from apps.dispatch import farm_in, farm_in_order
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead, VehicleType
from apps.notifications.models import Notification
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    vt.refresh_from_db()
    return vt


def _new(payload=None, tid=TID):
    return farm_in.handle_new(payload or sample_payload(), transaction_id=tid, delivery_id="d-1")


def _req() -> FarmInRequest:
    return FarmInRequest.objects.select_related("lead", "reservation").get(transaction_id=TID)


def test_new_creates_lead_reservation_and_envelope(sedan, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        body = _new()
    req = _req()
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert Lead.objects.count() == 1 and Reservation.objects.count() == 1
    assert req.lead == Lead.objects.get() and req.reservation == Reservation.objects.get()
    assert req.reservation.lead == req.lead and req.lead.status == Lead.Status.NEW
    assert req.reservation.stops.count() == 3
    assert req.reservation.named_passengers.count() == 1
    assert req.reservation.vehicle == sedan and req.gnet_vehicle_type == "SEDAN"
    assert req.company.gnet_grid_id == "gnettest" and req.company_created
    assert req.requester_res_no == "1715016686" and req.gnet_status == "BOOKING_REQUEST"
    assert req.payload["locations"]["pickup"]["time"] == "2024-08-03T10:27:22"
    assert req.pickup_at == datetime(2024, 8, 3, 17, 27, 22, tzinfo=UTC)
    assert req.timezone == "America/Los_Angeles" and req.desk == "reservations"
    assert req.alert == ""
    n = Notification.objects.get(kind=Notification.Kind.FARM_IN_NEW)
    assert n.farm_in_request_id == req.pk and n.lead_id is None
    assert n.detail.startswith("123 S Beverly Dr, Beverly Hills, CA 90212, USA → ")
    assert n.detail.endswith("(LAX)")


def test_new_unmapped_leaves_no_vehicle_and_omits_total(sedan):
    p = sample_payload()
    p["preferredVehicleType"] = "BLIMP"
    body = _new(p)
    req = _req()
    assert body == {"reservationId": f"FI-{req.pk}"}
    res = req.reservation
    assert res.vehicle is None and res.rate == 0 and res.min_hours == 0
    assert res.line_total == Decimal("0.00") and res.stops.count() == 3
    assert req.alert == "Unmapped vehicle type: BLIMP"


def test_new_build_failure_leaves_no_rows(client, settings):
    settings.GNET_CALLBACK_SECRET = secret = "farminsecret"
    settings.GNET_FARM_IN_ENABLED = True
    raw = json.dumps(sample_payload()).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    with patch.object(farm_in_order, "build", side_effect=RuntimeError("boom")):
        r = client.post(
            reverse("gnet_farm_in"),
            data=raw,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {secret}",
            HTTP_X_LANSDOWNE_SIGNATURE=sig,
            HTTP_X_LANSDOWNE_EVENT=farm_in.EVENT_NEW,
            HTTP_X_LANSDOWNE_TRANSACTION_ID=TID,
            HTTP_X_LANSDOWNE_DELIVERY_ID="d-1",
        )
    assert r.json() == {"error": "internal error"}
    assert not FarmInRequest.objects.exists() and not Company.objects.exists()
    assert not Lead.objects.exists() and not Reservation.objects.exists()


def test_new_replay_is_identical_and_creates_nothing(sedan, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = _new()
        second = _new()
    assert first == second
    assert FarmInRequest.objects.count() == 1 and Company.objects.count() == 1
    assert Lead.objects.count() == 1 and Reservation.objects.count() == 1
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_NEW).count() == 1


def test_new_replays_even_when_row_is_declined(sedan):
    first = _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.DECLINED)
    assert _new() == first and FarmInRequest.objects.count() == 1


@pytest.mark.parametrize(
    "where", [("status",), ("affiliateReservation", "action"), ("affiliateReservation", "status")]
)
def test_cancel_arriving_as_new_is_flagged(sedan, where):
    first = _new()
    FarmInRequest.objects.update(last_event_at=datetime(2024, 1, 1, tzinfo=UTC))
    p = sample_payload()
    target = p if len(where) == 1 else p[where[0]]
    target[where[-1]] = "cancel"
    assert _new(p) == first
    req = FarmInRequest.objects.get()
    assert req.alert == "Cancel received as a new delivery, review"
    assert req.last_event_at > datetime(2024, 1, 1, tzinfo=UTC)
    assert req.status == FarmInRequest.Status.PENDING


def test_cancel_as_new_on_canceled_row_changes_nothing(sedan):
    first = _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.CANCELED, alert="")
    p = sample_payload()
    p["status"] = "CANCEL"
    assert _new(p) == first
    assert FarmInRequest.objects.get().alert == ""


def test_new_reuses_existing_company(sedan):
    from apps.contacts.factories import CompanyFactory

    c = CompanyFactory(name="GNet Test", gnet_grid_id="gnettest")
    _new()
    req = FarmInRequest.objects.get()
    assert req.company == c and not req.company_created


def test_new_rejects_overlong_requester_id(sedan):
    p = sample_payload()
    p["affiliateReservation"]["requesterId"] = "x" * 70
    assert _new(p) == {"error": "requesterId too long"}
    assert FarmInRequest.objects.count() == 0 and Company.objects.count() == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_new_produces_one_row(restore_seeded_rows, sedan):
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            results.append(_new())
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results[0] == results[1]
    assert FarmInRequest.objects.count() == 1 and Company.objects.count() == 1
    assert Lead.objects.count() == 1 and Reservation.objects.count() == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_new_different_transactions_same_unknown_requester(restore_seeded_rows, sedan):
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker(tid):
        try:
            barrier.wait()
            results.append(_new(tid=tid))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in ("tx-a", "tx-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 2
    assert all("error" not in body for body in results)
    assert results[0]["reservationId"] != results[1]["reservationId"]
    assert FarmInRequest.objects.count() == 2 and Company.objects.count() == 1
    assert Lead.objects.count() == 2 and Reservation.objects.count() == 2


def test_quote_priced_and_persists_nothing(sedan):
    body = farm_in.handle_quote(sample_payload(), transaction_id=TID)
    assert body == {"reservationId": TID, "totalAmount": "165.00"}
    assert FarmInRequest.objects.count() == 0 and Lead.objects.count() == 0


def test_quote_creates_no_company(sedan):
    farm_in.handle_quote(sample_payload(), transaction_id=TID)
    assert Company.objects.count() == 0


def test_quote_unpriced():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=0)
    body = farm_in.handle_quote(sample_payload(), transaction_id=TID)
    assert body == {"error": "No rate on file for SEDAN"}


def _update(payload, tid=TID):
    return farm_in.handle_update(payload, transaction_id=tid, delivery_id="d-2")


def test_update_unknown_transaction():
    assert _update(sample_payload(), tid="nope") == {"error": "unknown transaction"}


def test_update_rewrites_stops_and_passengers(sedan):
    _new()
    p = sample_payload()
    p["locations"]["stops"] = []
    p["locations"]["pickup"]["time"] = "2024-08-04T08:15:00"
    p["passengers"] = [
        {"firstName": "Dana", "lastName": "Lee"},
        {"firstName": "Sam", "lastName": "Ray"},
    ]
    p["passengerCount"] = "3"
    p["notes"] = [{"context": "AFFILIATE", "text": "New flight"}]
    body = _update(p)
    req = _req()
    res = req.reservation
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    stops = list(res.stops.order_by("sequence"))
    assert len(stops) == 2 and stops[0].address.startswith("123 S Beverly Dr")
    assert stops[1].address.endswith("(LAX)")
    assert [r.name for r in res.named_passengers.all()] == ["Dana Lee", "Sam Ray"]
    assert res.passengers == 3
    assert res.pickup_date == date(2024, 8, 4) and res.pickup_time == time(8, 15)
    assert req.pickup_at == datetime(2024, 8, 4, 15, 15, tzinfo=UTC)
    assert req.lead.notes.splitlines() == ["New flight", "GNet res no 1715016686"]
    assert req.payload["passengerCount"] == "3" and req.alert == "Updated by requester"
    assert Reservation.objects.count() == 1 and Lead.objects.count() == 1


def test_update_keeps_agent_pricing_and_vehicle(sedan):
    _new()
    suv = VehicleTypeFactory(name="Executive SUV")
    Reservation.objects.update(
        vehicle=suv,
        rate=Decimal("200"),
        hours=3,
        gratuity_pct=20,
        discount_pct=10,
        discount_flat=Decimal("5"),
    )
    FarmInRequest.objects.update(desk=FarmInRequest.Desk.DISPATCH)
    p = sample_payload()
    p["locations"]["stops"].append({"address": "500 Rodeo Dr", "stopNumber": "2"})
    body = _update(p)
    res = _req().reservation
    assert res.stops.count() == 4
    assert res.vehicle == suv and res.rate == Decimal("200.00") and res.hours == 3
    assert res.gratuity_pct == 20 and res.min_hours == Decimal("1")
    assert res.discount_pct == 10 and res.discount_flat == Decimal("5.00")
    assert _req().desk == FarmInRequest.Desk.DISPATCH
    assert body["totalAmount"] == f"{res.line_total:.2f}"


def test_update_fills_vehicle_only_when_trip_had_none(sedan):
    p = sample_payload()
    p["preferredVehicleType"] = "BLIMP"
    _new(p)
    body = _update(sample_payload())
    req = _req()
    res = req.reservation
    assert res.vehicle == sedan and res.rate == Decimal("165.00") and res.min_hours == 1
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert req.alert == "Updated by requester" and req.gnet_vehicle_type == "SEDAN"


def test_update_alerts_when_requester_changes_vehicle_after_agent_chose(sedan):
    _new()
    p = sample_payload()
    p["preferredVehicleType"] = "SUV"
    _update(p)
    req = _req()
    assert req.reservation.vehicle == sedan and req.reservation.rate == Decimal("165.00")
    assert req.alert == "Updated by requester · Requester now asks for SUV"
    assert req.gnet_vehicle_type == "SUV"
    _update(p)
    assert _req().alert == "Requester now asks for SUV · Updated by requester"


def test_update_unmapped_code_on_a_trip_with_a_vehicle_is_not_flagged_unmapped(sedan):
    _new()
    p = sample_payload()
    p["preferredVehicleType"] = "BLIMP"
    _update(p)
    req = _req()
    assert req.reservation.vehicle == sedan
    assert req.alert == "Updated by requester · Requester now asks for BLIMP"


def test_cancel_before_accept_marks_lead_lost(sedan, django_capture_on_commit_callbacks):
    _new()
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = _req()
    assert body == {"reservationId": f"FI-{req.pk}"}
    assert req.status == FarmInRequest.Status.CANCELED and req.gnet_status == "CANCEL"
    assert req.lead.status == Lead.Status.LOST and req.lead.lost_reason == "Requester canceled"
    assert not req.updated_after_accept
    assert not Notification.objects.filter(kind=Notification.Kind.FARM_IN_UPDATED).exists()


def test_cancel_before_accept_on_an_already_lost_lead_keeps_its_reason(sedan):
    _new()
    Lead.objects.update(status=Lead.Status.LOST, lost_reason="Too far")
    p = sample_payload()
    p["status"] = "CANCEL"
    _update(p)
    req = _req()
    assert req.status == FarmInRequest.Status.CANCELED and req.lead.lost_reason == "Too far"


def test_cancel_on_a_booked_lead_before_accept_flags_for_review(
    sedan, django_capture_on_commit_callbacks
):
    _new()
    Lead.objects.update(status=Lead.Status.BOOKED)
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = _req()
    assert body == {"reservationId": f"FI-{req.pk}"}
    assert req.status == FarmInRequest.Status.CANCELED
    assert req.lead.status == Lead.Status.BOOKED
    assert req.alert == "Requester canceled, review the order"
    n = Notification.objects.get(kind=Notification.Kind.FARM_IN_UPDATED)
    assert n.farm_in_request_id == req.pk


def test_cancel_after_accept_leaves_order_booked(sedan, django_capture_on_commit_callbacks):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.ACCEPTED)
    Lead.objects.update(status=Lead.Status.BOOKED)
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        _update(p)
    req = _req()
    assert req.status == FarmInRequest.Status.CANCELED and req.updated_after_accept
    assert req.alert == "Requester canceled"
    assert req.lead.status == Lead.Status.BOOKED
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_UPDATED).exists()


def test_update_after_accept_stores_and_flags(sedan, django_capture_on_commit_callbacks):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.ACCEPTED)
    p = sample_payload()
    p["passengerCount"] = "4"
    p["locations"]["stops"] = []
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = _req()
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert req.updated_after_accept and req.payload["passengerCount"] == "4"
    assert req.reservation.passengers == 2 and req.reservation.stops.count() == 3
    assert req.alert == "Updated by requester after accept, review the order"
    n = Notification.objects.get(kind=Notification.Kind.FARM_IN_UPDATED)
    assert n.farm_in_request_id == req.pk


def test_update_cancel_on_declined_is_refused(sedan):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.DECLINED)
    p = sample_payload()
    p["status"] = "CANCEL"
    assert _update(p) == {"error": "request is declined"}
    assert FarmInRequest.objects.get().status == FarmInRequest.Status.DECLINED


def test_update_cancel_on_canceled_is_idempotent(sedan, django_capture_on_commit_callbacks):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.CANCELED, updated_after_accept=True)
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = FarmInRequest.objects.get()
    assert body == {"reservationId": f"FI-{req.pk}"}
    assert req.delivery_id == "d-1" and req.gnet_status == "BOOKING_REQUEST"
    assert not Notification.objects.filter(kind=Notification.Kind.FARM_IN_UPDATED).exists()


@pytest.mark.parametrize("status", ["declined", "canceled", "expired"])
def test_update_on_closed_request(sedan, status):
    _new()
    FarmInRequest.objects.update(status=status)
    assert _update(sample_payload()) == {"error": f"request is {status}"}


def test_receive_coerces_non_string_transaction_id(sedan):
    body = farm_in.receive(
        farm_in.EVENT_NEW, sample_payload(), transaction_id=12345, delivery_id="d"
    )
    assert body["reservationId"].startswith("FI-")
    assert FarmInRequest.objects.get().transaction_id == "12345"


def test_receive_dispatch():
    assert farm_in.receive("nope", {}, transaction_id=TID, delivery_id="d") == {
        "error": "unsupported event nope"
    }
    assert farm_in.receive(farm_in.EVENT_NEW, {}, transaction_id="", delivery_id="d") == {
        "error": "transaction id missing"
    }
    assert farm_in.receive(farm_in.EVENT_NEW, {}, transaction_id="  ", delivery_id="d") == {
        "error": "transaction id missing"
    }
