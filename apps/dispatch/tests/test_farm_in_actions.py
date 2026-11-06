import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department, UserDepartment
from apps.dispatch import farm_in, farm_in_actions
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.farm_in_actions import FarmInActionError
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent():
    return UserFactory()


@pytest.fixture
def ops_user():
    user = UserFactory()
    UserDepartment.objects.create(user=user, department=Department.OPERATIONS)
    return user


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    vt.refresh_from_db()
    return vt


@pytest.fixture
def req():
    return FarmInRequestFactory(payload=sample_payload(), gnet_vehicle_type="SEDAN")


def test_price_sets_manual_amount_and_who(req, agent):
    out = farm_in_actions.price(req.pk, user=agent, amount=Decimal("210"), note="holiday")
    assert out.quoted_amount == Decimal("210.00")
    assert out.pricing_source == FarmInRequest.PricingSource.MANUAL
    assert out.priced_by == agent and out.pricing_note == "holiday"


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-5")])
def test_price_refuses_zero_or_negative(req, agent, amount):
    with pytest.raises(FarmInActionError, match="Enter a price above zero"):
        farm_in_actions.price(req.pk, user=agent, amount=amount)


def test_price_refuses_decided_request_with_409(agent):
    done = FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.price(done.pk, user=agent, amount=Decimal("10"))
    assert exc.value.status == 409


def test_set_vehicle_reprices_from_rate_card(req, agent, sedan):
    req.alert = farm_in.ALERT_UNMAPPED.format(code="SEDAN")
    req.save()
    out = farm_in_actions.set_vehicle(req.pk, user=agent, vehicle_id=sedan.pk)
    assert out.vehicle == sedan and out.quoted_amount == Decimal("165.00")
    assert out.pricing_source == FarmInRequest.PricingSource.RATE_CARD
    assert out.alert == ""


def test_set_vehicle_keeps_manual_price(req, agent, sedan):
    req.quoted_amount = Decimal("200")
    req.pricing_source = FarmInRequest.PricingSource.MANUAL
    req.save()
    out = farm_in_actions.set_vehicle(req.pk, user=agent, vehicle_id=sedan.pk)
    assert out.vehicle == sedan and out.quoted_amount == Decimal("200.00")
    assert out.pricing_source == FarmInRequest.PricingSource.MANUAL


def test_set_vehicle_with_no_rate_leaves_amount_none(req, agent, sedan):
    VehicleType.objects.filter(pk=sedan.pk).update(rate=0)
    out = farm_in_actions.set_vehicle(req.pk, user=agent, vehicle_id=sedan.pk)
    assert out.quoted_amount is None and out.pricing_source == ""


def test_set_vehicle_refuses_unknown_vehicle(req, agent):
    with pytest.raises(FarmInActionError, match="Unknown vehicle"):
        farm_in_actions.set_vehicle(req.pk, user=agent, vehicle_id=0)


def test_request_coverage_moves_desk_and_notifies_operations(
    req, agent, ops_user, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        out = farm_in_actions.request_coverage(req.pk, user=agent)
    assert out.desk == FarmInRequest.Desk.DISPATCH and out.desk_changed_by == agent
    assert out.desk_changed_at is not None
    notes = Notification.objects.filter(kind=Notification.Kind.FARM_IN_COVERAGE)
    assert [n.user for n in notes] == [ops_user]
    assert notes[0].farm_in_request_id == req.pk
    assert notes[0].title == f"Coverage requested: {req.company.name}"


def test_request_coverage_falls_back_to_shared_notification(
    req, agent, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        farm_in_actions.request_coverage(req.pk, user=agent)
    note = Notification.objects.get(kind=Notification.Kind.FARM_IN_COVERAGE)
    assert note.user is None


def test_request_coverage_refuses_when_already_with_dispatch(agent):
    with_dispatch = FarmInRequestFactory(desk=FarmInRequest.Desk.DISPATCH)
    with pytest.raises(FarmInActionError, match="Already with dispatch"):
        farm_in_actions.request_coverage(with_dispatch.pk, user=agent)


def test_send_back_requires_note_and_notifies_requesting_user(
    req, agent, ops_user, django_capture_on_commit_callbacks
):
    farm_in_actions.request_coverage(req.pk, user=agent)
    with pytest.raises(FarmInActionError, match="Say why it is going back"):
        farm_in_actions.send_back(req.pk, user=ops_user, note="  ")
    with django_capture_on_commit_callbacks(execute=True):
        out = farm_in_actions.send_back(req.pk, user=ops_user, note="No driver free")
    assert out.desk == FarmInRequest.Desk.RESERVATIONS and out.desk_note == "No driver free"
    assert out.desk_changed_by == ops_user
    note = Notification.objects.get(kind=Notification.Kind.FARM_IN_SENT_BACK)
    assert note.user == agent and note.detail == "No driver free"
    assert note.title == f"Sent back: {req.company.name}"


def test_send_back_refuses_from_reservations_desk(req, agent):
    with pytest.raises(FarmInActionError, match="Already with reservations"):
        farm_in_actions.send_back(req.pk, user=agent, note="x")


def test_decline_requires_reason_and_enqueues_reject(req, agent):
    with pytest.raises(FarmInActionError, match="Give the requester a reason"):
        farm_in_actions.decline(req.pk, user=agent, reason=" ")
    out = farm_in_actions.decline(req.pk, user=agent, reason="No cars")
    assert out.status == FarmInRequest.Status.DECLINED and out.decline_reason == "No cars"
    assert out.decided_by == agent and out.decided_at is not None
    assert FarmInStatusPush.objects.filter(request=req, status="REJECT").count() == 1


def test_decline_refuses_expired_with_400_and_accepted_with_409(agent):
    expired = FarmInRequestFactory(status=FarmInRequest.Status.EXPIRED)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.decline(expired.pk, user=agent, reason="x")
    assert exc.value.status == 400 and str(exc.value) == "Request is expired"
    accepted = FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.decline(accepted.pk, user=agent, reason="x")
    assert exc.value.status == 409 and str(exc.value) == "Already decided"


def test_retry_push_creates_fresh_pending_only_from_failed(req, agent):
    failed = FarmInStatusPushFactory(
        request=req, status="REJECT", state=FarmInStatusPush.State.FAILED
    )
    fresh = farm_in_actions.retry_push(failed.pk, user=agent)
    assert fresh.pk != failed.pk and fresh.state == FarmInStatusPush.State.PENDING
    with pytest.raises(FarmInActionError, match="A retry is already queued"):
        farm_in_actions.retry_push(failed.pk, user=agent)
    sent = FarmInStatusPushFactory(request=req, status="X", state=FarmInStatusPush.State.SENT)
    with pytest.raises(FarmInActionError):
        farm_in_actions.retry_push(sent.pk, user=agent)


@pytest.fixture(scope="module")
def restore_seeded_rows(django_db_blocker):
    """Put the migration-seeded rows back after a transactional test's teardown flush."""
    yield
    with django_db_blocker.unblock():
        connection.creation.deserialize_db_from_string(connection._test_serialized_contents)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_decide_race_second_actor_gets_409(restore_seeded_rows):
    user = UserFactory()
    target = FarmInRequestFactory(payload=sample_payload())
    barrier = threading.Barrier(2)
    wins, errors = [], []

    def worker():
        try:
            barrier.wait()
            wins.append(farm_in_actions.decline(target.pk, user=user, reason="No cars"))
        except FarmInActionError as exc:
            errors.append(exc)
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1 and len(errors) == 1
    assert isinstance(errors[0], FarmInActionError) and errors[0].status == 409
    assert FarmInStatusPush.objects.filter(request=target, status="REJECT").count() == 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"runType": "Hourly"}, True),
        ({"runType": "Airport"}, False),
        ({}, False),
    ],
)
def test_is_hourly_reads_run_type(payload, expected):
    assert farm_in.is_hourly(payload) is expected
