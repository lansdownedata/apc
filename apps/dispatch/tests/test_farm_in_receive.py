import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.contacts.models import Company
from apps.dispatch import farm_in
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.notifications.models import Notification

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


def test_new_creates_request_and_responds(sedan, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        body = _new()
    req = FarmInRequest.objects.get(transaction_id=TID)
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert req.company.gnet_grid_id == "gnettest" and req.company_created
    assert req.vehicle == sedan and req.pricing_source == FarmInRequest.PricingSource.RATE_CARD
    assert req.requester_res_no == "1715016686" and req.gnet_status == "BOOKING_REQUEST"
    assert req.payload["locations"]["pickup"]["time"] == "2024-08-03T10:27:22"
    assert req.timezone == "America/Los_Angeles" and req.desk == "reservations"
    n = Notification.objects.get(kind=Notification.Kind.FARM_IN_NEW)
    assert n.farm_in_request_id == req.pk and n.lead_id is None


def test_new_unpriced_omits_total_amount():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=0)
    body = _new()
    assert "totalAmount" not in body and body["reservationId"].startswith("FI-")
    assert FarmInRequest.objects.get().quoted_amount is None


def test_new_replay_is_identical_and_creates_nothing(sedan, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = _new()
        second = _new()
    assert first == second
    assert FarmInRequest.objects.count() == 1 and Company.objects.count() == 1
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_NEW).count() == 1


def test_new_replays_even_when_row_is_declined(sedan):
    first = _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.DECLINED)
    assert _new() == first and FarmInRequest.objects.count() == 1


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


# transaction=True flushes migration-seeded tables under --reuse-db; _test_serialized_contents
# is Django-private (set when a collected test asks for serialized_rollback).
@pytest.fixture(scope="module")
def restore_seeded_rows(django_db_blocker):
    """Put the migration-seeded rows back after a transactional test's teardown flush.

    The flush empties every table, fleet and airports included, and with `--reuse-db` that
    loss would outlive the run. `serialized_rollback=True` makes the test start from the
    serialized seed and skip post_migrate on flush; this module-scoped teardown runs after
    the flush and reloads the same snapshot.
    """
    yield
    with django_db_blocker.unblock():
        connection.creation.deserialize_db_from_string(connection._test_serialized_contents)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_new_produces_one_row(restore_seeded_rows, sedan):
    results, errors = [], []

    def worker():
        try:
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


def test_quote_priced_and_persists_nothing(sedan):
    body = farm_in.handle_quote(sample_payload(), transaction_id=TID)
    assert body == {"reservationId": TID, "totalAmount": "165.00"}
    assert FarmInRequest.objects.count() == 0


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


def test_update_pending_rederives_but_keeps_manual_price(sedan):
    _new()
    req = FarmInRequest.objects.get()
    req.quoted_amount = Decimal("200")
    req.pricing_source = "manual"
    req.save()
    p = sample_payload()
    p["passengerCount"] = "3"
    p["affiliateReservation"]["action"] = "UPDATE"
    body = _update(p)
    req.refresh_from_db()
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "200.00"}
    assert req.passenger_count == 3 and req.alert == "Updated by requester"
    assert req.payload["passengerCount"] == "3"


def test_update_pending_appends_summary_alerts(sedan):
    _new()
    p = sample_payload()
    p["preferredVehicleType"] = "BLIMP"
    body = _update(p)
    req = FarmInRequest.objects.get()
    assert body == {"reservationId": f"FI-{req.pk}"}
    assert req.alert == "Updated by requester · Unmapped vehicle type: BLIMP"
    assert req.vehicle is None and req.quoted_amount is None and req.pricing_source == ""


def test_update_cancel_pending(sedan, django_capture_on_commit_callbacks):
    _new()
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = FarmInRequest.objects.get()
    assert body == {"reservationId": f"FI-{req.pk}"}
    assert req.status == FarmInRequest.Status.CANCELED and req.gnet_status == "CANCEL"
    assert not req.updated_after_accept
    assert not Notification.objects.filter(kind=Notification.Kind.FARM_IN_UPDATED).exists()


def test_update_cancel_after_accept_flags_only(sedan, django_capture_on_commit_callbacks):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.ACCEPTED)
    p = sample_payload()
    p["status"] = "CANCEL"
    with django_capture_on_commit_callbacks(execute=True):
        _update(p)
    req = FarmInRequest.objects.get()
    assert req.status == FarmInRequest.Status.CANCELED and req.updated_after_accept
    assert req.alert == "Requester canceled"
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_UPDATED).exists()


def test_update_after_accept_stores_and_flags(sedan, django_capture_on_commit_callbacks):
    _new()
    FarmInRequest.objects.update(status=FarmInRequest.Status.ACCEPTED)
    p = sample_payload()
    p["passengerCount"] = "4"
    with django_capture_on_commit_callbacks(execute=True):
        body = _update(p)
    req = FarmInRequest.objects.get()
    assert body == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert req.updated_after_accept and req.payload["passengerCount"] == "4"
    assert req.passenger_count == 2  # not re-derived after accept
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
