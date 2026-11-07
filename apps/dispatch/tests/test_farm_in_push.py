from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.core import cron
from apps.dispatch import farm_in_push
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.integrations import gnet
from apps.integrations.gnet import GnetAPIError
from apps.leads import services as lead_services
from apps.leads.models import Lead
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
Push = FarmInStatusPush


@pytest.fixture(autouse=True)
def gnet_settings(settings):
    settings.GNET_API_KEY = "k"
    settings.GNET_GATEWAY_URL = "https://gw.test"


def _response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.content = b"x" if json_data is not None else b""
    return resp


def _down(status=503):
    return patch.object(farm_in_push.gnet, "push_status", side_effect=GnetAPIError(status, "down"))


def test_enqueue_creates_pending_and_delivers_on_commit(django_capture_on_commit_callbacks):
    req = FarmInRequestFactory(alert="GNet CONFIRMED not delivered: old")
    with patch.object(gnet, "requests") as fake:
        fake.request.return_value = _response(200, {"ok": True})
        with django_capture_on_commit_callbacks(execute=True):
            push = farm_in_push.enqueue(req, "CONFIRMED", Decimal("165"))
    push.refresh_from_db()
    req.refresh_from_db()
    assert Push.objects.count() == 1
    assert push.state == Push.State.SENT and push.sent_at is not None and push.last_error == ""
    assert req.alert == ""


def test_success_keeps_another_statuss_failure_alert():
    req = FarmInRequestFactory(alert="GNet CONFIRMED not delivered: old")
    push = FarmInStatusPushFactory(request=req, status="ASSIGNED", state=Push.State.PENDING)
    with patch.object(farm_in_push.gnet, "push_status", return_value={}):
        farm_in_push.deliver(push)
    req.refresh_from_db()
    assert req.alert == "GNet CONFIRMED not delivered: old"


def test_enqueue_skips_when_pending_exists_for_status():
    req = FarmInRequestFactory()
    FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.PENDING)
    assert farm_in_push.enqueue(req, "CONFIRMED") is None
    assert Push.objects.count() == 1


def test_enqueue_skips_sent_status_unless_repeatable():
    req = FarmInRequestFactory()
    FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.SENT)
    FarmInStatusPushFactory(request=req, status="ASSIGNED", state=Push.State.SENT)
    assert farm_in_push.enqueue(req, "CONFIRMED") is None
    assert farm_in_push.enqueue(req, "ASSIGNED") is not None


def test_success_removes_only_its_own_failure_segment():
    req = FarmInRequestFactory(
        alert="No pickup time: set it on the order · GNet CONFIRMED not delivered: old"
    )
    push = FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.PENDING)
    with patch.object(farm_in_push.gnet, "push_status", return_value={}):
        farm_in_push.deliver(push)
    req.refresh_from_db()
    assert req.alert == "No pickup time: set it on the order"


def test_failure_adds_its_segment_and_keeps_the_others():
    req = FarmInRequestFactory(alert="Requester canceled · GNet CANCEL not delivered: old")
    push = FarmInStatusPushFactory(request=req, status="CANCEL", state=Push.State.PENDING)
    with _down(422):
        farm_in_push.deliver(push)
    req.refresh_from_db()
    assert req.alert == "Requester canceled · GNet CANCEL not delivered: GNet gateway 422: down"


def test_deliver_without_api_key_fails_with_clear_error(settings):
    settings.GNET_API_KEY = ""
    push = FarmInStatusPushFactory(state=Push.State.PENDING)
    with patch.object(gnet, "requests") as fake:
        farm_in_push.deliver(push)
        fake.request.assert_not_called()
    push.refresh_from_db()
    assert push.state == Push.State.FAILED and push.last_error == "GNET_API_KEY not set"
    assert push.request.alert.startswith("GNet CONFIRMED not delivered")
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_PUSH_FAILED).count() == 1


def test_deliver_retries_on_503_with_backoff():
    push = FarmInStatusPushFactory(state=Push.State.PENDING)
    before = timezone.now()
    with _down(503):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.state == Push.State.PENDING and push.attempts == 1
    assert before + timedelta(seconds=60) <= push.next_attempt_at
    assert push.next_attempt_at <= timezone.now() + timedelta(seconds=65)


def test_deliver_retries_on_transport_failure():
    push = FarmInStatusPushFactory(state=Push.State.PENDING)
    with _down(0):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.state == Push.State.PENDING and push.attempts == 1


def test_deliver_fails_on_422_and_alerts():
    push = FarmInStatusPushFactory(state=Push.State.PENDING)
    with _down(422):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.state == Push.State.FAILED and "422" in push.last_error
    assert "not delivered" in push.request.alert
    assert Notification.objects.filter(kind=Notification.Kind.FARM_IN_PUSH_FAILED).exists()


def test_backoff_caps_at_30_minutes():
    push = FarmInStatusPushFactory(state=Push.State.PENDING, attempts=5)
    with _down(503):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.attempts == 6
    assert push.next_attempt_at <= timezone.now() + timedelta(minutes=30, seconds=5)
    assert push.next_attempt_at > timezone.now() + timedelta(minutes=29)


def test_eighth_attempt_fails():
    push = FarmInStatusPushFactory(state=Push.State.PENDING, attempts=7)
    with _down(503):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.state == Push.State.FAILED and push.attempts == 8


def test_cron_delivers_due_pushes_only():
    now = timezone.now()
    due = FarmInStatusPushFactory(state=Push.State.PENDING, next_attempt_at=now)
    later = FarmInStatusPushFactory(
        state=Push.State.PENDING, next_attempt_at=now + timedelta(minutes=5)
    )
    with patch.object(farm_in_push.gnet, "push_status", return_value={}) as fake:
        assert farm_in_push.run_cron() == 1
    assert fake.call_count == 1
    due.refresh_from_db()
    later.refresh_from_db()
    assert due.state == Push.State.SENT and later.state == Push.State.PENDING


def test_cron_expires_pending_requests_an_hour_past_pickup():
    now = timezone.now()
    old = FarmInRequestFactory(pickup_at=now - timedelta(minutes=61))
    recent = FarmInRequestFactory(pickup_at=now - timedelta(minutes=30))
    accepted = FarmInRequestFactory(
        pickup_at=now - timedelta(hours=5), status=FarmInRequest.Status.ACCEPTED
    )
    assert farm_in_push.run_cron() == 1
    for r in (old, recent, accepted):
        r.refresh_from_db()
    assert old.status == FarmInRequest.Status.EXPIRED
    assert old.alert == "Expired: pickup time passed"
    assert recent.status == FarmInRequest.Status.PENDING
    assert accepted.status == FarmInRequest.Status.ACCEPTED
    assert Push.objects.count() == 0


def test_cron_expiry_marks_lead_lost():
    now = timezone.now()
    old = FarmInRequestFactory(pickup_at=now - timedelta(minutes=61))
    recent = FarmInRequestFactory(pickup_at=now - timedelta(minutes=30))
    assert farm_in_push.run_cron() == 1
    old.lead.refresh_from_db()
    recent.lead.refresh_from_db()
    assert old.lead.status == Lead.Status.LOST
    assert old.lead.lost_reason == "GNet request expired"
    assert recent.lead.status == Lead.Status.NEW


def test_cron_expiry_skips_an_already_lost_lead():
    now = timezone.now()
    req = FarmInRequestFactory(pickup_at=now - timedelta(hours=2))
    Lead.objects.filter(pk=req.lead_id).update(status=Lead.Status.LOST, lost_reason="Too far")
    assert farm_in_push.run_cron() == 1
    req.refresh_from_db()
    req.lead.refresh_from_db()
    assert req.status == FarmInRequest.Status.EXPIRED
    assert req.lead.lost_reason == "Too far"


def test_cron_expiry_on_a_booked_lead_flags_for_review(django_capture_on_commit_callbacks):
    now = timezone.now()
    req = FarmInRequestFactory(pickup_at=now - timedelta(hours=2))
    Lead.objects.filter(pk=req.lead_id).update(status=Lead.Status.BOOKED)
    with django_capture_on_commit_callbacks(execute=True):
        assert farm_in_push.run_cron() == 1
    req.refresh_from_db()
    req.lead.refresh_from_db()
    assert req.status == FarmInRequest.Status.EXPIRED
    assert req.alert == "Expired, review the order"
    assert req.lead.status == Lead.Status.BOOKED
    n = Notification.objects.get(kind=Notification.Kind.FARM_IN_UPDATED)
    assert n.farm_in_request_id == req.pk


def test_cron_expiry_survives_a_row_that_fails():
    now = timezone.now()
    bad = FarmInRequestFactory(pickup_at=now - timedelta(hours=3))
    good = FarmInRequestFactory(pickup_at=now - timedelta(hours=2))
    real = lead_services.mark_lost

    def mark_lost(lead, reason):
        if lead.pk == bad.lead_id:
            raise RuntimeError("touch-points down")
        return real(lead, reason)

    with patch.object(lead_services, "mark_lost", side_effect=mark_lost):
        assert farm_in_push.run_cron() == 2
    bad.refresh_from_db()
    good.refresh_from_db()
    good.lead.refresh_from_db()
    assert bad.status == FarmInRequest.Status.PENDING
    assert good.status == FarmInRequest.Status.EXPIRED
    assert good.lead.status == Lead.Status.LOST


def test_retry_creates_fresh_pending_from_failed():
    failed = FarmInStatusPushFactory(
        status="ASSIGNED", state=Push.State.FAILED, total_amount=Decimal("10.00"), attempts=8
    )
    fresh = farm_in_push.retry(failed)
    assert fresh.pk != failed.pk and fresh.state == Push.State.PENDING and fresh.attempts == 0
    assert fresh.status == "ASSIGNED" and fresh.total_amount == Decimal("10.00")


def test_retry_refuses_when_status_already_delivered():
    req = FarmInRequestFactory()
    FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.SENT)
    failed = FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.FAILED)
    with pytest.raises(ValueError, match="Already delivered"):
        farm_in_push.retry(failed)


def test_retry_refuses_when_pending_exists():
    req = FarmInRequestFactory()
    FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.PENDING)
    failed = FarmInStatusPushFactory(request=req, status="CONFIRMED", state=Push.State.FAILED)
    with pytest.raises(ValueError, match="A retry is already queued"):
        farm_in_push.retry(failed)


def test_retry_allows_repeatable_status_after_sent():
    req = FarmInRequestFactory()
    FarmInStatusPushFactory(request=req, status="ASSIGNED", state=Push.State.SENT)
    failed = FarmInStatusPushFactory(request=req, status="ASSIGNED", state=Push.State.FAILED)
    assert farm_in_push.retry(failed).state == Push.State.PENDING


def test_deliver_on_sent_push_makes_no_call():
    push = FarmInStatusPushFactory(state=Push.State.SENT)
    with patch.object(farm_in_push.gnet, "push_status") as fake:
        farm_in_push.deliver(push)
    fake.assert_not_called()
    push.refresh_from_db()
    assert push.state == Push.State.SENT


def test_cron_skips_a_push_claimed_by_another_attempt():
    now = timezone.now()
    FarmInStatusPushFactory(state=Push.State.PENDING, next_attempt_at=now - timedelta(seconds=1))
    with patch.object(farm_in_push.gnet, "push_status", side_effect=GnetAPIError(503, "x")):
        farm_in_push.deliver(Push.objects.get())
        with patch.object(farm_in_push.gnet, "push_status") as fake:
            assert farm_in_push.run_cron() == 0
    fake.assert_not_called()


def test_deliver_claims_with_a_lease_before_the_call():
    push = FarmInStatusPushFactory(state=Push.State.PENDING, next_attempt_at=timezone.now())
    seen = {}

    def spy(*args, **kwargs):
        seen["next"] = Push.objects.get(pk=push.pk).next_attempt_at
        return {}

    with patch.object(farm_in_push.gnet, "push_status", side_effect=spy):
        farm_in_push.deliver(push)
    assert seen["next"] > timezone.now() + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("status", "retries"),
    [(408, True), (429, True), (500, True), (400, False), (404, False)],
)
def test_status_matrix_boundaries(status, retries):
    push = FarmInStatusPushFactory(state=Push.State.PENDING)
    with _down(status):
        farm_in_push.deliver(push)
    push.refresh_from_db()
    assert push.state == (Push.State.PENDING if retries else Push.State.FAILED)


def test_retry_refuses_non_failed_push():
    with pytest.raises(ValueError, match="Only a failed push can be retried"):
        farm_in_push.retry(FarmInStatusPushFactory(state=Push.State.PENDING))


def test_cron_job_is_registered():
    assert cron.JOBS["gnet-farm-in"] is farm_in_push.run_cron
