"""The request frame: every entry a request writes carries its actor, source and one cid."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from django.urls import resolve, reverse

from apps.accounts.factories import UserFactory
from apps.audit.context import audit_context
from apps.audit.context import resolve as resolve_frame
from apps.audit.middleware import AuditMiddleware, source_for
from apps.audit.models import AuditEntry
from apps.core import cron
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _trip_entries():
    ct = ContentType.objects.get_for_model(Reservation)
    return AuditEntry.objects.filter(content_type=ct)


def test_staff_post_stamps_actor_portal_and_one_cid(client):
    user = UserFactory()
    outbound = ReservationFactory()
    client.force_login(user)
    before = set(AuditEntry.objects.values_list("pk", flat=True))

    client.post(reverse("reservation_return", args=[outbound.pk]))

    entries = list(AuditEntry.objects.exclude(pk__in=before))
    assert entries
    assert {e.actor_id for e in entries} == {user.pk}
    assert {e.source for e in entries} == {"portal"}
    cids = {e.cid for e in entries}
    assert len(cids) == 1
    assert cids.pop().startswith("r-")


def test_cron_source(client, settings):
    settings.CRON_SECRET = "s3cret"
    made = []
    with patch.dict(cron.JOBS, {"charge-due-balances": lambda: made.append(ReservationFactory())}):
        resp = client.post("/cron/charge-due-balances/", HTTP_X_CRON_KEY="s3cret")

    assert resp.status_code == 200
    entry = _trip_entries().get(object_id=made[0].pk)
    assert entry.source == "cron: charge-due-balances"
    assert entry.actor is None


def test_correlation_header_ignored(client):
    client.force_login(UserFactory())
    outbound = ReservationFactory()
    before = set(AuditEntry.objects.values_list("pk", flat=True))

    client.post(reverse("reservation_return", args=[outbound.pk]), HTTP_X_CORRELATION_ID="evil")

    cids = set(AuditEntry.objects.exclude(pk__in=before).values_list("cid", flat=True))
    assert cids
    assert "evil" not in cids


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/portal/", "portal"),
        ("/portal/leads/", "portal"),
        ("/portal/login/", "portal"),
        ("/admin/", "admin"),
        ("/admin/login/", "admin"),
        ("/cron/charge-due-balances/", "cron: charge-due-balances"),
        ("/webhooks/stripe/", "webhook: stripe"),
        ("/webhooks/podium/", "webhook: podium"),
        ("/webhooks/limoanywhere/tok/", "webhook: limoanywhere"),
        ("/webhooks/calendly/", "webhook: calendly"),
        ("/api/gnet/farm-in", "webhook: gnet"),
        ("/api/gnet/farm-in/", "webhook: gnet"),
        ("/api/gnet/callback", "webhook: gnet"),
        ("/quote/tok/", "customer: quote page"),
        ("/quote/tok/pay/", "customer: quote page"),
        ("/quote/tok/terms/", "customer: quote page"),
        ("/quote/tok/done/", "customer: quote page"),
        ("/trip/tok/", "customer: trip confirmation"),
        ("/trip/o/tok/", "customer: trip confirmation"),
        ("/trip/w/tok/", "customer: trip confirmation"),
        ("/wedding/tok/details/", "customer: wedding details"),
        ("/trip/affiliate/tok/", "affiliate: trip confirmation"),
        ("/planner/", "planner portal"),
        ("/planner/login/", "planner portal"),
        ("/invite/uid/tok/", "invite"),
        ("/", "public site"),
        ("/healthz/", "web: health"),
    ],
)
def test_source_for_table(path, expected):
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    assert source_for(request) == expected


def test_source_for_unresolved_request_uses_path():
    request = RequestFactory().get("/no/such/page/")
    request.resolver_match = None
    assert source_for(request) == "web: /no/such/page/"


def test_planner_request_has_email_no_actor():
    planner = SimpleNamespace(email="ana@example.com")
    request = RequestFactory().get("/planner/")
    request.user = AnonymousUser()
    request.planner = planner
    request.resolver_match = resolve("/planner/")
    seen = {}

    def view(req):
        seen["resolved"] = resolve_frame()
        return None

    AuditMiddleware(view)(request)

    assert seen["resolved"] == (None, "ana@example.com", "planner portal")


def test_staff_actor_email_defaults_to_user_email():
    user = UserFactory()
    request = RequestFactory().get("/portal/")
    request.user = user
    request.resolver_match = resolve("/portal/")
    seen = {}

    def view(req):
        seen["resolved"] = resolve_frame()

    AuditMiddleware(view)(request)

    assert seen["resolved"] == (user, user.email, "portal")


def test_frame_is_closed_after_request():
    request = RequestFactory().get("/portal/")
    request.user = AnonymousUser()
    AuditMiddleware(lambda req: None)(request)
    with audit_context(source="outer"):
        pass
    assert resolve_frame()[2] in {"system"} or resolve_frame()[2].startswith("command:")
