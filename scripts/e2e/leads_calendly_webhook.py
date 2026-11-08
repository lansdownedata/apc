"""Post a signed Calendly invitee webhook to the local receiver (/webhooks/calendly/).

Booking a real call through /schedule/book/ would create an invitee on the client's live
Calendly account, so the Calendly lead path is tested by posting what Calendly would send.
The receiver (`apps/integrations/views.py::_calendly_signature_ok`) wants
`Calendly-Webhook-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>." + raw body>` keyed with
CALENDLY_WEBHOOK_SIGNING_KEY (blank key = accepts unsigned). No timestamp window.

The booking's event type is CALENDLY_EVENT_TYPE_URI from .env, so it is not filtered out;
--other-event-type sends a different one (expect it ignored: no lead, no CalendlyEvent row).
Every send uses a fresh invitee uri unless you pass --invitee-uri; the uri sent is printed,
which is what --cancel and --reschedule-of take.

Examples (repo root, venv active, runserver on :8000):
  python scripts/e2e/leads_calendly_webhook.py --dry-run
  python scripts/e2e/leads_calendly_webhook.py --name "Cal Booker" --email you+cal@example.com \\
      --phone +12025550160 --answer "What is the occasion?=Prom, 12 people" --utm google/cpc/spring
  python scripts/e2e/leads_calendly_webhook.py --cancel <invitee uri printed above>
  python scripts/e2e/leads_calendly_webhook.py --reschedule-of <uri> --email you+cal@example.com
  python scripts/e2e/leads_calendly_webhook.py --reject bad-signature        # expect 403
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from _bootstrap import BASE_URL, django_ready, env  # noqa: F401  (no DB access needed)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send a signed Calendly invitee.created / invitee.canceled webhook to "
        "{BASE_URL}/webhooks/calendly/. Touches no database directly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples" + __doc__.split("Examples", 1)[1],
    )
    p.add_argument("--name", default="E2E Calendly Booker")
    p.add_argument("--email", default="e2e+calendly@example.com")
    p.add_argument("--phone", default="", help="text_reminder_number (E.164), optional")
    p.add_argument(
        "--start",
        help="call start, ISO UTC (default tomorrow 18:30Z)",
    )
    p.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="Q=A",
        help="a questions_and_answers row; repeatable",
    )
    p.add_argument("--utm", metavar="SRC/MED/CAMPAIGN", help="tracking utm_source/medium/campaign")
    p.add_argument("--guest", action="append", default=[], help="an event guest email; repeatable")
    p.add_argument("--invitee-uri", help="invitee uri to send (default: a fresh one)")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--cancel", metavar="URI", help="send invitee.canceled for this invitee")
    action.add_argument(
        "--reschedule-of",
        metavar="URI",
        help="send invitee.created whose old_invitee is URI (a moved call)",
    )
    p.add_argument(
        "--rescheduled",
        action="store_true",
        help="with --cancel: mark it rescheduled (the receiver then writes no note)",
    )
    p.add_argument(
        "--other-event-type",
        action="store_true",
        help="use a different event type uri (the receiver must ignore it)",
    )
    p.add_argument(
        "--reject",
        choices=("unsigned", "bad-signature"),
        help="send without the header, or signed with the wrong key (both expect 403 "
        "while CALENDLY_WEBHOOK_SIGNING_KEY is set)",
    )
    p.add_argument("--base-url", default=BASE_URL, help=f"default {BASE_URL} (or $E2E_BASE_URL)")
    p.add_argument("--dry-run", action="store_true", help="print the signed request, send nothing")
    return p.parse_args()


def fresh_uri() -> str:
    event = uuid.uuid4().hex[:16].upper()
    invitee = uuid.uuid4().hex[:16].upper()
    return f"https://api.calendly.com/scheduled_events/E2E{event}/invitees/E2E{invitee}"


def build(args) -> dict:
    event_type = env("CALENDLY_EVENT_TYPE_URI") or "https://api.calendly.com/event_types/E2E"
    if args.other_event_type:
        event_type = "https://api.calendly.com/event_types/E2E-SOMETHING-ELSE"
    if args.cancel:
        return {
            "event": "invitee.canceled",
            "payload": {
                "uri": args.cancel,
                "name": args.name,
                "email": args.email,
                "rescheduled": bool(args.rescheduled),
                "scheduled_event": {"event_type": event_type},
            },
        }
    start = (
        datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        if args.start
        else (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=18, minute=30, second=0, microsecond=0
        )
    )
    uri = args.invitee_uri or fresh_uri()
    answers = []
    for raw in args.answer:
        question, _, answer = raw.partition("=")
        answers.append({"question": question.strip(), "answer": answer.strip()})
    tracking = {}
    if args.utm:
        parts = (args.utm.split("/") + ["", "", ""])[:3]
        tracking = dict(zip(("utm_source", "utm_medium", "utm_campaign"), parts, strict=True))
    iso = lambda d: d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")  # noqa: E731
    return {
        "event": "invitee.created",
        "payload": {
            "uri": uri,
            "name": args.name,
            "email": args.email,
            "timezone": "America/New_York",
            "text_reminder_number": args.phone,
            "questions_and_answers": answers,
            "tracking": tracking,
            "cancel_url": "https://calendly.com/cancellations/e2e",
            "reschedule_url": "https://calendly.com/reschedulings/e2e",
            "rescheduled": False,
            "old_invitee": args.reschedule_of,
            "scheduled_event": {
                "uri": uri.split("/invitees/")[0],
                "name": "Quick Chat",
                "start_time": iso(start),
                "end_time": iso(start + timedelta(minutes=15)),
                "status": "active",
                "event_type": event_type,
                "location": {"type": "outbound_call", "location": args.phone or "+1 202-555-0160"},
                "event_guests": [{"email": g} for g in args.guest],
            },
        },
    }


def main() -> None:
    args = parse_args()
    payload = build(args)
    raw = json.dumps(payload).encode()
    key = env("CALENDLY_WEBHOOK_SIGNING_KEY")
    headers = {"Content-Type": "application/json"}
    if args.reject != "unsigned" and key:
        signing_key = "wrong-key" if args.reject == "bad-signature" else key
        ts = str(int(time.time()))
        digest = hmac.new(signing_key.encode(), f"{ts}.".encode() + raw, hashlib.sha256)
        headers["Calendly-Webhook-Signature"] = f"t={ts},v1={digest.hexdigest()}"
    elif not key:
        print("# CALENDLY_WEBHOOK_SIGNING_KEY is blank: the receiver accepts unsigned posts.")
    url = f"{args.base_url.rstrip('/')}/webhooks/calendly/"
    print(f"POST {url}")
    print(f"# event {payload['event']} · invitee uri {payload['payload']['uri']}")
    if args.dry_run:
        for k, v in headers.items():
            print(f"{k}: {v}")
        print(json.dumps(payload, indent=2))
        return
    import requests

    resp = requests.post(url, data=raw, headers=headers, timeout=30)
    print(f"HTTP {resp.status_code} {resp.text[:200]}")


if __name__ == "__main__":
    main()
