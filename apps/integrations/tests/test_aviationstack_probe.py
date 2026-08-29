"""`manage.py aviationstack_probe` — one-off live probe (requests mocked here)."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError, call_command

from apps.integrations.management.commands import aviationstack_probe as probe

ARGS = ["--airline", "UA", "--flight", "123", "--airport", "IAD", "--date", "2026-10-15"]


def test_settings_default_to_disabled(settings):
    from django.conf import settings as dj

    assert hasattr(dj, "AVIATIONSTACK_API_KEY")
    assert dj.AVIATIONSTACK_BASE_URL == "https://api.aviationstack.com"


def test_probe_refuses_to_run_without_a_key(settings):
    settings.AVIATIONSTACK_API_KEY = ""
    with pytest.raises(CommandError, match="AVIATIONSTACK_API_KEY"):
        call_command("aviationstack_probe", *ARGS)


def test_probe_hits_flights_future_for_a_far_date_and_saves_the_body(settings, tmp_path):
    settings.AVIATIONSTACK_API_KEY = "k"
    resp = MagicMock(status_code=200, text="{}")
    resp.json.return_value = {"data": [[{"flight": {"iataNumber": "UA123"}}]]}
    out = StringIO()
    with (
        patch.object(probe, "requests") as req,
        patch.object(probe, "PROBE_DIR", tmp_path),
        patch.object(probe, "today", return_value=probe.date(2026, 8, 29)),
    ):
        req.get.return_value = resp
        call_command("aviationstack_probe", *ARGS, stdout=out)
    url = req.get.call_args.args[0]
    params = req.get.call_args.kwargs["params"]
    assert url.endswith("/v1/flightsFuture")
    assert params["iataCode"] == "IAD" and params["type"] == "arrival"
    assert params["date"] == "2026-10-15" and params["flight_number"] == "123"
    assert params["access_key"] == "k"
    assert "access_key" not in out.getvalue().split("HTTP")[0].replace("redacted", "")
    saved = json.loads((tmp_path / "2026-10-15-flightsFuture-UA123-arrival.json").read_text())
    assert saved["data"][0][0]["flight"]["iataNumber"] == "UA123"


def test_probe_uses_flights_for_a_near_date(settings, tmp_path):
    settings.AVIATIONSTACK_API_KEY = "k"
    resp = MagicMock(status_code=200, text="{}")
    resp.json.return_value = {"data": []}
    with (
        patch.object(probe, "requests") as req,
        patch.object(probe, "PROBE_DIR", tmp_path),
        patch.object(probe, "today", return_value=probe.date(2026, 10, 12)),
    ):
        req.get.return_value = resp
        call_command("aviationstack_probe", *ARGS, stdout=StringIO())
    assert req.get.call_args.args[0].endswith("/v1/flights")
    assert req.get.call_args.kwargs["params"]["flight_iata"] == "UA123"
