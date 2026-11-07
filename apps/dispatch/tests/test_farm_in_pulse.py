"""APC-136 — the pulse the shell polls to notice a new GNet request."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.dispatch.factories import FarmInRequestFactory
from apps.dispatch.models import FarmInRequest
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db

URL = "/portal/farm-in/pulse/"
K = Notification.Kind


def _arrival(kind=K.FARM_IN_NEW, **req_fields) -> Notification:
    req = FarmInRequestFactory(**req_fields)
    return Notification.notify(
        req.lead, kind, title=f"{kind} title", detail="Acme Limo", farm_in_request=req
    )


def test_pulse_url_is_named():
    assert reverse("farm_in_pulse") == URL


def test_pulse_requires_login(client):
    resp = client.get(URL)
    assert resp.status_code == 302
    assert "login" in resp.url


def test_pulse_shape_and_counts(logged_in_client):
    n = _arrival()
    FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED)
    data = logged_in_client.get(URL).json()
    assert data["pending"] == 1
    assert data["latest_id"] == n.pk
    assert [a["id"] for a in data["arrivals"]][0] == n.pk
    first = data["arrivals"][0]
    assert set(first) == {"id", "title", "detail", "dispatch", "lead", "request"}
    assert first["dispatch"] is False
    assert first["lead"] == n.farm_in_request.lead_id
    assert first["request"] == n.farm_in_request_id


def test_pulse_empty_has_null_latest(logged_in_client):
    data = logged_in_client.get(URL).json()
    assert data == {"pending": 0, "latest_id": None, "arrivals": []}


def test_pulse_since_filters_already_seen_arrivals(logged_in_client):
    old = _arrival()
    new = _arrival()
    ids = [a["id"] for a in logged_in_client.get(URL, {"since": old.pk}).json()["arrivals"]]
    assert ids == [new.pk]
    assert logged_in_client.get(URL, {"since": "junk"}).json()["arrivals"]


def test_pulse_marks_coverage_as_dispatch(logged_in_client):
    _arrival(kind=K.FARM_IN_COVERAGE)
    _arrival(kind=K.FARM_IN_SENT_BACK)
    arrivals = logged_in_client.get(URL).json()["arrivals"]
    assert [a["dispatch"] for a in arrivals] == [True]


def test_pulse_is_two_queries(logged_in_client):
    for _ in range(3):
        _arrival()
    with CaptureQueriesContext(connection) as ctx:
        resp = logged_in_client.get(URL)
    assert resp.status_code == 200
    # The session and the user are the request's own cost; the pulse is the count + arrivals.
    pulse = [
        q
        for q in ctx.captured_queries
        if "django_session" not in q["sql"] and "FROM `accounts_user`" not in q["sql"]
    ]
    assert len(pulse) == 2, [q["sql"] for q in pulse]
