"""Golden recording: what every automated message sends today, word for word (APC-118).

Recorded once on the engine as it stood before the one-messaging-model rewrite (HEAD
46a2af0). `expected.json` is never re-recorded after that: a failure here means the new code
changed what a customer, billing contact or affiliate receives, or who receives it.

Each scenario (golden/scenarios.py) builds a fixed order on a frozen clock and returns the
TouchPoint under test with the clock at its send time. `capture` runs the `run-touchpoints`
cron job once and records every send: Podium texts and emails, Podium review invites, and
any email handed to Django's mail backend.

Record (once only): GOLDEN_RECORD=1 pytest apps/messaging/tests/test_golden.py
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core import mail

from apps.messaging.models import TouchPoint
from apps.messaging.tests.golden import scenarios

pytestmark = pytest.mark.django_db

EXPECTED = Path(__file__).parent / "golden" / "expected.json"
RECORD = os.environ.get("GOLDEN_RECORD") == "1"
SEND = "apps.integrations.podium.send_message"
INVITE = "apps.integrations.podium.create_review_invitation"
REVIEW_LINK = "https://podium.test/r/golden-review"


@pytest.fixture(autouse=True)
def _live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = scenarios.BASE_URL
    settings.TIME_ZONE = "America/New_York"
    settings.COMPANY_NAME = "All Pro Charter"
    settings.COMPANY_PHONE = "(703) 555-0100"
    settings.COMPANY_EMAIL = "reservations@allprocharter.com"


def capture(tp: TouchPoint) -> list[dict]:
    """Run the cron once at the scenario's moment; every send it makes, in order."""
    calls: list[dict] = []

    def send(*, identifier, body, channel_type="phone", **_):
        calls.append({"channel_type": channel_type, "identifier": identifier, "body": body})
        return {"uid": f"golden-{len(calls)}"}

    def invite(*, phone, first_name, last_name, **_):
        calls.append({"review_invite": phone, "first_name": first_name, "last_name": last_name})
        return {"url": REVIEW_LINK, "uid": "golden-invite"}

    mailed = len(mail.outbox)
    with patch(SEND, side_effect=send), patch(INVITE, side_effect=invite):
        scenarios.run_cron()
    for message in mail.outbox[mailed:]:
        calls.append(
            {"email_to": list(message.to), "subject": message.subject, "body": message.body}
        )
    return calls


def _normalised(call: dict) -> dict:
    return {k: scenarios.normalise(v) if isinstance(v, str) else v for k, v in call.items()}


def _outcome(name: str) -> dict:
    with (
        scenarios.frozen(),
        # Sends made while a scenario plays its clock forward are setup, not the golden.
        patch(SEND, return_value={"uid": "setup"}),
        patch(INVITE, return_value={"url": REVIEW_LINK, "uid": "setup"}),
    ):
        tp = scenarios.build()[name]()
        calls = capture(tp)
    tp.refresh_from_db()
    return {
        "kind": tp.kind,
        "status": tp.status,
        "error": tp.error,
        # Order across channels/recipients within one run carries no meaning.
        "calls": sorted(
            (_normalised(c) for c in calls), key=lambda c: json.dumps(c, sort_keys=True)
        ),
    }


def _expected() -> dict:
    return json.loads(EXPECTED.read_text()) if EXPECTED.exists() else {}


@pytest.mark.parametrize("name", sorted(scenarios.build()))
def test_golden(name):
    got = _outcome(name)
    if RECORD:
        recorded = _expected()
        recorded[name] = got
        EXPECTED.write_text(
            json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return
    want = _expected()[name]
    assert got["calls"] == want["calls"]
    assert (got["kind"], got["status"], got["error"]) == (
        want["kind"],
        want["status"],
        want["error"],
    )


@pytest.mark.skipif(RECORD, reason="recording")
def test_every_scenario_and_every_kind_is_recorded():
    expected = _expected()
    assert set(expected) == set(scenarios.build())
    assert {s["kind"] for s in expected.values()} >= set(TouchPoint.Kind.values)


@pytest.mark.skipif(RECORD, reason="recording")
def test_the_recording_is_real_copy():
    """No blank body, no "None", no unrendered placeholder; nothing sent means a reason."""
    for name, outcome in _expected().items():
        if not outcome["calls"]:
            assert outcome["status"] != TouchPoint.Status.SENT and outcome["error"], name
        for call in outcome["calls"]:
            text = " ".join(str(v) for v in call.values())
            assert "None" not in text, name
            assert not re.search(r"\{\w+\}", text), name
            if "body" in call:
                assert call["body"].strip(), name
