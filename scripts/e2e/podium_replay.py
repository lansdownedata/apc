"""Replay a Podium webhook to the local receiver, signed the way the receiver checks it.

The receiver (`apps/integrations/views.py::_podium_signature_ok`) wants two headers:
`Podium-Timestamp` and `Podium-Signature` = hex HMAC-SHA256 of "<timestamp>." + raw body,
keyed with PODIUM_WEBHOOK_SECRET. There is no timestamp window, so any timestamp works.

The template is a real payload stored in `integrations_podiumevent.payload` (latest one
whose metadata.eventType is message.received, or `--event PK`). Every replay gets a fresh
metadata.eventUid and data.uid, so neither the event dedupe nor the message dedupe eats it.
The texter's identity is NEVER the stored one unless you pass --keep-identity (those rows
are real customers from the live location): by default it is a fictional 202-555-01xx
number, named "E2E Texter". The Podium contact uid and conversation uid are derived from
the phone, so the same --phone always lands in the same local conversation.

Examples (repo root, venv active, runserver on :8000):
  python scripts/e2e/podium_replay.py --dry-run
  python scripts/e2e/podium_replay.py --phone +12025550111 --name "Dana Texter" \
      --body "Need a bus Sat"
  python scripts/e2e/podium_replay.py --new-contact            # random 555-01xx -> new thread
  python scripts/e2e/podium_replay.py --as message.failed --message-uid latest-outbound
  python scripts/e2e/podium_replay.py --reject bad-signature   # expect 403
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import random
import sys
import time
import uuid
from datetime import UTC, datetime

from _bootstrap import BASE_URL, django_ready, env  # noqa: F401  (env kept for parity)

DEFAULT_PHONE = "+12025550100"
DEFAULT_NAME = "E2E Texter"
EVENT_TYPES = ("message.received", "message.sent", "message.failed")

# Used only when the PodiumEvent table is empty — the shape of a real 2026-07 payload.
BUILTIN = {
    "data": {
        "uid": "",
        "body": "",
        "items": None,
        "sender": {"uid": None},
        "contact": {"uid": "", "name": "", "externalIdentifier": None},
        "location": {"uid": "", "organizationUid": None},
        "createdAt": "",
        "senderUid": None,
        "webchatUrl": None,
        "contactName": "",
        "conversation": {
            "uid": "",
            "channel": {"type": "phone", "identifier": ""},
            "startedAt": "",
            "assignedUserUid": None,
        },
        "attachmentUrl": None,
        "failureReason": None,
    },
    "metadata": {"eventUid": "", "eventType": "message.received"},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay a signed Podium webhook (message.received by default) to "
        "{BASE_URL}/webhooks/podium/. Reads the dev DB for the template; writes nothing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples" + __doc__.split("Examples", 1)[1],
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--event", type=int, metavar="PK", help="PodiumEvent pk to use as template")
    src.add_argument(
        "--latest",
        action="store_true",
        help="latest stored event whose metadata.eventType is message.received (default)",
    )
    p.add_argument(
        "--as",
        dest="event_type",
        choices=EVENT_TYPES,
        default="message.received",
        help="metadata.eventType to send (default message.received)",
    )
    p.add_argument("--phone", help=f"texter's number (default {DEFAULT_PHONE})")
    p.add_argument(
        "--new-contact",
        action="store_true",
        help="use a random fictional 202-555-01xx number, so a NEW contact + conversation is made",
    )
    p.add_argument("--name", default=DEFAULT_NAME, help="contact name Podium reports")
    p.add_argument("--body", help="message text (default: a timestamped E2E line)")
    p.add_argument(
        "--keep-identity",
        action="store_true",
        help="keep the stored payload's phone/name/contact uid/body (real customer data!)",
    )
    p.add_argument(
        "--message-uid",
        help="data.uid to send instead of a fresh one. 'latest-outbound' = the newest "
        "outbound Message with a Podium uid (use with --as message.failed / message.sent)",
    )
    p.add_argument(
        "--failure-reason",
        default="Delivery not possible (E2E replay)",
        help="data.failureReason for --as message.failed",
    )
    p.add_argument(
        "--reject",
        choices=("unsigned", "bad-signature", "bad-json"),
        help="send a request the receiver must refuse: no headers (403), wrong key (403), "
        "or a correctly signed non-JSON body (400)",
    )
    p.add_argument("--base-url", default=BASE_URL, help=f"default {BASE_URL} (or $E2E_BASE_URL)")
    p.add_argument("--dry-run", action="store_true", help="print the signed request, send nothing")
    return p.parse_args()


def template(args) -> tuple[dict, str]:
    from apps.integrations.models import PodiumEvent

    if args.event:
        row = PodiumEvent.objects.filter(pk=args.event).first()
        if row is None:
            sys.exit(f"No PodiumEvent with pk={args.event}.")
        return copy.deepcopy(row.payload), f"PodiumEvent #{row.pk}"
    # The event_type column is unreliable on old rows (all say message.received), so pick
    # by what the payload itself says.
    for row in PodiumEvent.objects.order_by("-pk").only("pk", "payload"):
        meta = (row.payload or {}).get("metadata") or {}
        if meta.get("eventType") == "message.received":
            return copy.deepcopy(row.payload), f"PodiumEvent #{row.pk} (latest message.received)"
    return copy.deepcopy(BUILTIN), "built-in template (no stored message.received)"


def stable_uid(kind: str, phone: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"apc-e2e-podium:{kind}:{phone}"))


def unused_phone() -> str:
    """A fictional 202-555-01xx number no local contact holds yet (so the thread is new)."""
    from apps.contacts.models import Contact

    for n in random.sample(range(100, 200), 100):
        phone = f"+1202555{n:04d}"
        if Contact.objects.find_match(phone=phone) is None:
            return phone
    sys.exit("Every 202-555-01xx number is taken locally; pass --phone instead.")


def latest_outbound_uid() -> str:
    from apps.messaging.models import Message

    msg = (
        Message.objects.filter(direction=Message.Direction.OUT)
        .exclude(podium_message_uid="")
        .order_by("-created_at")
        .first()
    )
    if msg is None:
        sys.exit("No outbound Message with a podium_message_uid yet — send from the inbox first.")
    print(
        f"# using Message #{msg.pk} (conversation {msg.conversation_id}) "
        f"uid {msg.podium_message_uid}"
    )
    return msg.podium_message_uid


def build(args) -> tuple[dict, str]:
    from django.conf import settings

    payload, source = template(args)
    payload.setdefault("metadata", {})
    payload.setdefault("data", {})
    data = payload["data"]
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    payload["metadata"]["eventUid"] = str(uuid.uuid4())
    payload["metadata"]["eventType"] = args.event_type
    if args.message_uid == "latest-outbound":
        data["uid"] = latest_outbound_uid()
    else:
        data["uid"] = args.message_uid or str(uuid.uuid4())
    data["createdAt"] = now
    data["location"] = {
        **(data.get("location") or {}),
        "uid": settings.PODIUM_LOCATION_UID or (data.get("location") or {}).get("uid"),
    }
    data["failureReason"] = args.failure_reason if args.event_type == "message.failed" else None

    if not args.keep_identity:
        if args.new_contact:
            phone = unused_phone()
        else:
            phone = args.phone or DEFAULT_PHONE
        convo = data.setdefault("conversation", {})
        convo["uid"] = stable_uid("conversation", phone)
        convo["channel"] = {"type": "phone", "identifier": phone}
        convo["startedAt"] = now
        data["contact"] = {
            **(data.get("contact") or {}),
            "uid": stable_uid("contact", phone),
            "name": args.name,
        }
        data["contactName"] = args.name
        data["body"] = args.body or f"E2E replay {now[:19]}: hi, can I get a quote for Saturday?"
        if args.event_type == "message.received":
            data["sender"], data["senderUid"] = {"uid": None}, None
    elif args.body:
        data["body"] = args.body
    return payload, source


def sign(secret: str, timestamp: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()


def main() -> None:
    args = parse_args()
    django_ready()
    from django.conf import settings

    secret = settings.PODIUM_WEBHOOK_SECRET
    payload, source = build(args)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json"}

    if args.reject == "bad-json":
        raw = b"not json {"
    if args.reject != "unsigned":
        key = secret if args.reject != "bad-signature" else f"wrong-{secret or 'key'}"
        headers["Podium-Timestamp"] = timestamp
        headers["Podium-Signature"] = sign(key, timestamp, raw)
    if not secret:
        print("# WARNING: PODIUM_WEBHOOK_SECRET is blank — the receiver accepts anything.")

    url = args.base_url.rstrip("/") + "/webhooks/podium/"
    print(f"# template: {source}")
    print(f"POST {url}")
    for k, v in headers.items():
        print(f"{k}: {v}")
    print()
    try:
        print(json.dumps(json.loads(raw), indent=2))
    except ValueError:
        print(raw.decode(errors="replace"))

    if args.dry_run:
        print("\n# --dry-run: nothing sent.")
        return

    import requests

    resp = requests.post(url, data=raw, headers=headers, timeout=30)
    print(f"\n# -> HTTP {resp.status_code} {resp.text[:200]!r}")
    if resp.status_code != 200:
        return
    from apps.integrations.models import PodiumEvent

    event = PodiumEvent.objects.filter(event_uid=payload["metadata"]["eventUid"]).first()
    if event is None:
        print("# no PodiumEvent row found for this eventUid (different DB?)")
        return
    print(
        f"# PodiumEvent #{event.pk} processed={event.processed} "
        f"conversation={event.conversation_id}"
    )
    if event.conversation_id:
        print(
            f"# open: {args.base_url.rstrip('/')}/portal/inbox/"
            f"?conversation={event.conversation_id}"
        )


if __name__ == "__main__":
    main()
