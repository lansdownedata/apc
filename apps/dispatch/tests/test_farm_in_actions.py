import threading
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department, UserDepartment
from apps.dispatch import farm_in, farm_in_actions, farm_in_push
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.farm_in_actions import FarmInActionError
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.models import Lead
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
def req():
    return FarmInRequestFactory(payload=sample_payload(), gnet_vehicle_type="SEDAN")


def test_desk_actions_lock_only_the_request_row(req, agent):
    """Postgres refuses FOR UPDATE on the nullable side of an outer join."""
    with CaptureQueriesContext(connection) as ctx:
        farm_in_actions.request_coverage(req.pk, user=agent)
    locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
    assert locks and all("FOR UPDATE OF" in sql for sql in locks)


def test_decided_request_refuses_with_409(agent):
    done = FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED)
    with pytest.raises(FarmInActionError) as exc:
        farm_in_actions.request_coverage(done.pk, user=agent)
    assert exc.value.status == 409


def test_request_coverage_moves_desk_and_notifies_operations(
    req, agent, ops_user, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        out = farm_in_actions.request_coverage(req.pk, user=agent)
    out.refresh_from_db()
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


def test_request_coverage_skips_inactive_operations_users(
    req, agent, ops_user, django_capture_on_commit_callbacks
):
    ops_user.is_active = False
    ops_user.save()
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
    out.refresh_from_db()
    assert out.desk == FarmInRequest.Desk.RESERVATIONS and out.desk_note == "No driver free"
    assert out.desk_changed_by == ops_user
    note = Notification.objects.get(kind=Notification.Kind.FARM_IN_SENT_BACK)
    assert note.user == agent and note.detail == "No driver free"
    assert note.title == f"Sent back: {req.company.name}"


def test_send_back_to_inactive_requester_goes_shared(
    req, agent, ops_user, django_capture_on_commit_callbacks
):
    farm_in_actions.request_coverage(req.pk, user=agent)
    agent.is_active = False
    agent.save()
    with django_capture_on_commit_callbacks(execute=True):
        farm_in_actions.send_back(req.pk, user=ops_user, note="No driver free")
    assert Notification.objects.get(kind=Notification.Kind.FARM_IN_SENT_BACK).user is None


def test_send_back_refuses_from_reservations_desk(req, agent):
    with pytest.raises(FarmInActionError, match="Already with reservations"):
        farm_in_actions.send_back(req.pk, user=agent, note="x")


def test_decline_marks_lead_lost_with_reason_and_enqueues_reject(req, agent):
    with pytest.raises(FarmInActionError, match="Give the requester a reason"):
        farm_in_actions.decline(req.pk, user=agent, reason=" ")
    out = farm_in_actions.decline(req.pk, user=agent, reason="No cars")
    out.refresh_from_db()
    assert out.status == FarmInRequest.Status.DECLINED and out.decline_reason == "No cars"
    assert out.decided_by == agent and out.decided_at is not None
    assert out.lead.status == Lead.Status.LOST and out.lead.lost_reason == "No cars"
    assert FarmInStatusPush.objects.filter(request=req, status="REJECT").count() == 1


def test_decline_on_already_lost_lead_still_declines(req, agent):
    Lead.objects.filter(pk=req.lead_id).update(status=Lead.Status.LOST, lost_reason="Earlier")
    out = farm_in_actions.decline(req.pk, user=agent, reason="No cars")
    out.refresh_from_db()
    assert out.status == FarmInRequest.Status.DECLINED
    assert out.lead.lost_reason == "Earlier"
    assert FarmInStatusPush.objects.filter(request=req, status="REJECT").count() == 1


def test_decline_refuses_a_booked_lead_and_changes_nothing(req, agent):
    Lead.objects.filter(pk=req.lead_id).update(status=Lead.Status.BOOKED)
    with pytest.raises(FarmInActionError, match="Lead is booked"):
        farm_in_actions.decline(req.pk, user=agent, reason="No cars")
    req.refresh_from_db()
    assert req.status == FarmInRequest.Status.PENDING
    assert not FarmInStatusPush.objects.exists()


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
    target.refresh_from_db()
    assert target.status == FarmInRequest.Status.DECLINED
    assert target.decided_by == user and target.decline_reason == "No cars"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_accept_and_decline_race_one_wins(restore_seeded_rows, settings):
    settings.GNET_API_KEY = "k"
    user = UserFactory()
    target = FarmInRequestFactory(payload=sample_payload())
    barrier = threading.Barrier(2)
    wins, errors = [], []

    def worker(action):
        try:
            barrier.wait()
            wins.append(action())
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    actions = [
        lambda: farm_in_actions.accept(target.pk, user=user),
        lambda: farm_in_actions.decline(target.pk, user=user, reason="No cars"),
    ]
    with patch.object(farm_in_push, "deliver"):
        threads = [threading.Thread(target=worker, args=(a,)) for a in actions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(wins) == 1 and len(errors) == 1
    assert isinstance(errors[0], FarmInActionError) and errors[0].status == 409
    codes = list(FarmInStatusPush.objects.filter(request=target).values_list("status", flat=True))
    assert codes in (["CONFIRMED"], ["REJECT"])
    target.refresh_from_db()
    expected = "CONFIRMED" if target.status == FarmInRequest.Status.ACCEPTED else "REJECT"
    assert codes == [expected]


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
