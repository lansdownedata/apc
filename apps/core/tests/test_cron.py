"""POST /cron/<job>/ — secret-header-gated dispatcher for scheduled jobs."""

from unittest.mock import patch

from apps.core import cron

URL = "/cron/charge-due-balances/"


def test_missing_header_is_403(client, settings):
    settings.CRON_SECRET = "s3cret"
    assert client.post(URL).status_code == 403


def test_wrong_key_is_403(client, settings):
    settings.CRON_SECRET = "s3cret"
    assert client.post(URL, HTTP_X_CRON_KEY="nope").status_code == 403


def test_unset_secret_fails_closed(client, settings):
    settings.CRON_SECRET = ""
    assert client.post(URL, HTTP_X_CRON_KEY="").status_code == 403


def test_unknown_job_is_404(client, settings):
    settings.CRON_SECRET = "s3cret"
    resp = client.post("/cron/not-a-job/", HTTP_X_CRON_KEY="s3cret")
    assert resp.status_code == 404


def test_get_is_405(client, settings):
    settings.CRON_SECRET = "s3cret"
    assert client.get(URL, HTTP_X_CRON_KEY="s3cret").status_code == 405


def test_valid_call_runs_job_and_returns_count(client, settings):
    settings.CRON_SECRET = "s3cret"
    with patch.dict(cron.JOBS, {"charge-due-balances": lambda: 5}):
        resp = client.post(URL, HTTP_X_CRON_KEY="s3cret")
    assert resp.status_code == 200
    assert resp.json() == {"job": "charge-due-balances", "processed": 5}


def test_registry_contains_both_jobs():
    assert set(cron.JOBS) == {
        "charge-due-balances",
        "recognize-due-revenue",
        "retry-la-sync",
    }


def test_registry_contains_retry_la_sync():
    assert "retry-la-sync" in cron.JOBS
