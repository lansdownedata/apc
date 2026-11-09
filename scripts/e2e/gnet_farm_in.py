"""Post GNet gateway deliveries to the local portal, signed the way the receivers check them.

Farm-in (`/api/gnet/farm-in`, `apps/integrations/views.py::gnet_farm_in`) wants
`Authorization: Bearer <GNET_CALLBACK_SECRET>` AND `X-Lansdowne-Signature: sha256=<hex HMAC-SHA256
of the raw body>` keyed with the same secret, plus `X-Lansdowne-Event` and
`X-Lansdowne-Transaction-Id` (the idempotency key). The receiver refuses everything until
GNET_FARM_IN_ENABLED=True and GNET_CALLBACK_SECRET is set in .env (runserver restarted).

The farm-out callback (`/api/gnet/callback`, no trailing slash, as the gateway posts it) uses
the same two credentials; with GNET_CALLBACK_SECRET blank it accepts unsigned posts (dev).

The secret is read from .env (GNET_CALLBACK_SECRET), the same file runserver reads. Nothing
here ever talks to the real gateway or GNet: it only posts to --base-url (default
http://127.0.0.1:8000, or $E2E_BASE_URL).

Examples (repo root, venv active):
  python scripts/e2e/gnet_farm_in.py grids        # griddIDs, requests, GNet assignments
  python scripts/e2e/gnet_farm_in.py probe                          # GET -> {"success": true}
  python scripts/e2e/gnet_farm_in.py new --dry-run                  # print the signed request only
  python scripts/e2e/gnet_farm_in.py new --grid gnettest --days 3 --time 10:30
  python scripts/e2e/gnet_farm_in.py new --tid e2e-abc              # run twice: same FI-<pk> back
  python scripts/e2e/gnet_farm_in.py update --tid e2e-abc --time 11:15 --pax 4
  python scripts/e2e/gnet_farm_in.py cancel --tid e2e-abc
  python scripts/e2e/gnet_farm_in.py cancel --tid e2e-abc --as-new  # cancel sent as farm_in.new
  python scripts/e2e/gnet_farm_in.py quote --vehicle SUV
  python scripts/e2e/gnet_farm_in.py new --bad-signature            # expect 403
  python scripts/e2e/gnet_farm_in.py callback --assignment 12 --status CONFIRMED --amount 180.00
  python scripts/e2e/gnet_farm_in.py callback --non-utf8            # APC-138: expect 400
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from _bootstrap import BASE_URL, django_ready, env

FARM_IN_PATH = "/api/gnet/farm-in"
CALLBACK_PATH = "/api/gnet/callback"  # no slash: what the gateway posts (APC-138)

# Route presets. `tz` is only used to work out the local pickup DATE from --days; the portal
# resolves the trip's zone itself from the pickup coordinates (or the airport table).
ROUTES = {
    "la": {
        "tz": "America/Los_Angeles",
        "offset": "-07:00",
        "pickup": {
            "locationType": "address",
            "address": "123 S Beverly Dr, Beverly Hills, CA 90212, USA",
            "lat": 34.0664703,
            "lon": -118.3993243,
        },
        "stops": [
            {
                "locationType": "address",
                "address": "9876 Wilshire Blvd, Beverly Hills, CA 90210, USA",
                "lat": 34.0669,
                "lon": -118.4005,
            }
        ],
        "dropOff": {
            "locationType": "airport",
            "address": "LAX",
            "flightInfo": {"flightNumber": "123", "airlineCode": "UA"},
        },
    },
    "dc": {
        "tz": "America/New_York",
        "offset": "-05:00",
        "pickup": {
            "locationType": "address",
            "address": "1600 Pennsylvania Ave NW, Washington, DC 20500, USA",
            "lat": 38.8976763,
            "lon": -77.0365298,
        },
        "stops": [],
        "dropOff": {
            "locationType": "airport",
            "address": "DCA",
            "flightInfo": {"flightNumber": "456", "airlineCode": "AA"},
        },
    },
    # Pickup named only by its IATA code, no coordinates: the zone comes from the Airport table.
    "airport-pickup": {
        "tz": "America/Denver",
        "offset": "-07:00",
        "pickup": {
            "locationType": "airport",
            "address": "DEN",
            "flightInfo": {"flightNumber": "789", "airlineCode": "UA"},
        },
        "stops": [],
        "dropOff": {
            "locationType": "address",
            "address": "1701 Wynkoop St, Denver, CO 80202, USA",
            "lat": 39.7530,
            "lon": -104.9998,
        },
    },
}


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pickup_iso(route: dict, days: int, hhmm: str) -> str:
    """Naive local ISO time, as GNet sends it (`2024-08-03T10:27:22`)."""
    hour, minute = (int(p) for p in hhmm.split(":", 1))
    local_today = datetime.now(ZoneInfo(route["tz"])).date()
    day = local_today + timedelta(days=days)
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"


def build_reservation(args: argparse.Namespace, *, status: str, action: str, quote=False) -> dict:
    """A GNet reservation payload shaped like GNET-CONNECTION-GUIDE.md §1 / the test sample."""
    route = json.loads(json.dumps(ROUTES[args.route]))  # deep copy
    pickup = route["pickup"]
    pickup["time"] = _pickup_iso(route, args.days, args.time)
    if args.no_coords:
        for loc in (pickup, route["dropOff"], *route["stops"]):
            loc.pop("lat", None)
            loc.pop("lon", None)
    if args.hourly:
        run_type = "Hourly"
    elif "airport" in (pickup.get("locationType"), route["dropOff"].get("locationType")):
        run_type = "Airport"
    else:
        run_type = "Point To Point"
    payload = {
        "transactionId": args.tid,
        "reservationDate": route["offset"],
        "reservationType": "QUOTE" if quote else "REGULAR",
        "submitMode": "WAIT",
        "runType": run_type,
        "preferredVehicleType": args.vehicle,
        "passengerCount": str(args.pax),
        "notes": [
            {"context": "AFFILIATE", "text": args.note},
            {"context": "DRIVER", "text": "Meet at curb, call on arrival"},
        ],
        "namesignInstructions": args.sign_name,
        "specialInstructions": "- Carseat needed",
        "affiliateReservation": {
            "action": action,
            "status": status,
            "requesterId": args.grid,
            "requesterResNo": args.res_no or args.tid.replace("e2e-", "")[:10],
            "providerId": args.provider_id,
        },
        "passengers": [
            {
                "firstName": args.first,
                "lastName": args.last,
                "email": args.passenger_email,
                "phoneNumber": "+12025550147",
            }
        ],
        "locations": {"pickup": pickup, "stops": route["stops"], "dropOff": route["dropOff"]},
    }
    if status in ("CANCEL", "BOOKING_REQUEST") and action != "NEW":
        payload["status"] = status  # top-level status is what the receiver reads first
    return payload


def _send(method: str, url: str, headers: dict, body: bytes | None, dry_run: bool) -> int:
    print(f"{method} {url}")
    for key, value in headers.items():
        shown = value
        if key == "Authorization" and value.startswith("Bearer ") and len(value) > 14:
            shown = value[:11] + "…" + value[-3:]
        print(f"  {key}: {shown}")
    if body is not None:
        try:
            print("  body:", json.dumps(json.loads(body), indent=2).replace("\n", "\n  "))
        except (ValueError, UnicodeDecodeError):
            print(f"  body (raw bytes): {body!r}")
    if dry_run:
        print("\n--dry-run: nothing sent.")
        return 0
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            code, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        print(f"\nCould not reach {url}: {exc.reason}. Is runserver up?")
        return 2
    ms = int((time.monotonic() - started) * 1000)
    print(f"\n-> HTTP {code} in {ms} ms\n{text}")
    if ms >= 15000:
        print("WARNING: over the gateway's 15 s budget.")
    return 0 if code < 400 else 1


def _secret(args: argparse.Namespace) -> str:
    return args.secret if args.secret is not None else env("GNET_CALLBACK_SECRET")


def _headers(secret: str, body: bytes, *, event: str, tid: str, bad_signature: bool) -> dict:
    headers = {"Content-Type": "application/json", "X-Lansdowne-Event": event}
    if tid:
        headers["X-Lansdowne-Transaction-Id"] = tid
    headers["X-Lansdowne-Delivery-Id"] = f"e2e-{uuid.uuid4()}"
    headers["X-Lansdowne-Griddid"] = "LATtest"
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
        headers["X-Lansdowne-Signature"] = _sign(
            "wrong-" + secret if bad_signature else secret, body
        )
    return headers


def cmd_farm_in(args: argparse.Namespace) -> int:
    secret = _secret(args)
    if not secret:
        print(
            "GNET_CALLBACK_SECRET is blank in .env: the receiver answers "
            '{"error": "GNet farm-in is not configured"} to every delivery. Sending unsigned.'
        )
    kind = args.command
    if kind == "new":
        event, payload = (
            "farm_in.new",
            build_reservation(args, status="BOOKING_REQUEST", action="NEW"),
        )
    elif kind == "update":
        event = "farm_in.update"
        payload = build_reservation(args, status="BOOKING_REQUEST", action="UPDATE")
    elif kind == "cancel":
        event = "farm_in.new" if args.as_new else "farm_in.update"
        payload = build_reservation(args, status="CANCEL", action="CANCEL")
        payload["status"] = "CANCEL"
    else:  # quote
        event = "farm_in.quote"
        payload = build_reservation(args, status="BOOKING_REQUEST", action="NEW", quote=True)
    body = json.dumps(payload).encode()
    headers = _headers(secret, body, event=event, tid=args.tid, bad_signature=args.bad_signature)
    print(f"transaction id: {args.tid}   (reuse with --tid to replay / update / cancel)\n")
    return _send("POST", args.base_url.rstrip("/") + FARM_IN_PATH, headers, body, args.dry_run)


def cmd_probe(args: argparse.Namespace) -> int:
    return _send("GET", args.base_url.rstrip("/") + FARM_IN_PATH, {}, None, args.dry_run)


def cmd_callback(args: argparse.Namespace) -> int:
    secret = _secret(args)
    if args.non_utf8:
        body = b"\xff\xfe\xfd not utf-8"
        tid = args.transaction_id or ""
    else:
        payload: dict = {"status": args.status}
        if args.transaction_id:
            payload["transactionId"] = args.transaction_id
        if args.amount is not None:
            payload["totalAmount"] = args.amount  # always a string on the wire
        affiliate: dict = {"providerResNo": f"E2E-{uuid.uuid4().hex[:6]}"}
        if args.assignment:
            affiliate["requesterResNo"] = f"apc-{args.assignment}"
        if args.notes:
            affiliate["notes"] = [{"message": args.notes, "context": "AFFILIATE"}]
        payload["affiliateReservation"] = affiliate
        if not args.transaction_id and not args.assignment:
            sys.exit("callback needs --assignment PK (resNo correlation) or --transaction-id.")
        body = json.dumps(payload).encode()
        tid = args.transaction_id or ""
    event = {"FAILED": "farm_out.failed", "CLOSE": "farm_out.closed"}.get(
        args.status, "farm_out.status"
    )
    headers = _headers(secret, body, event=event, tid=tid, bad_signature=args.bad_signature)
    return _send("POST", args.base_url.rstrip("/") + CALLBACK_PATH, headers, body, args.dry_run)


def cmd_grids(args: argparse.Namespace) -> int:
    """Read-only: what the local DB has to aim deliveries at."""
    django_ready()
    from apps.contacts.models import Company
    from apps.dispatch.models import Assignment, FarmInRequest
    from apps.vendors.models import Vendor

    print("Companies with a GNet griddID (use as --grid; an unknown --grid auto-creates one):")
    for c in Company.objects.exclude(gnet_grid_id__isnull=True).order_by("name"):
        flag = "affiliate" if c.is_affiliate_account else "NOT flagged affiliate"
        print(f"  {c.gnet_grid_id:<20} {c.name}  (company #{c.pk}, {flag})")
    print("\nGNet-capable vendors (farm-out over GNet; griddID set in /admin/):")
    for v in Vendor.objects.exclude(gnet_grid_id="").order_by("name"):
        print(f"  {v.gnet_grid_id:<20} {v.name}  (vendor #{v.pk}, {v.status})")
    print("\nLatest farm-in requests:")
    for r in FarmInRequest.objects.select_related("company").order_by("-pk")[:10]:
        print(
            f"  FI-{r.pk:<5} tid={r.transaction_id:<40} {r.status:<9} desk={r.desk:<12} "
            f"lead=#{r.lead_id} trip=#{r.reservation_id} {r.company.name}"
        )
    print("\nLatest GNet-channel assignments (use as callback --assignment):")
    gnet = Assignment.objects.filter(channel=Assignment.Channel.GNET).select_related("vendor")
    for a in gnet.order_by("-pk")[:10]:
        print(
            f"  #{a.pk:<5} trip=#{a.reservation_id:<6} {a.status:<10} {a.vendor.name}  "
            f"txn={a.gnet_transaction_id or '-'}  payout={a.payout}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post signed GNet farm-in deliveries / farm-out callbacks to the local portal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples", 1)[1].join(["Examples", ""]),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def target(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base-url", default=BASE_URL, help=f"portal root (default {BASE_URL})")
        p.add_argument(
            "--secret", default=None, help="override GNET_CALLBACK_SECRET from .env (testing only)"
        )
        p.add_argument("--dry-run", action="store_true", help="print the request, send nothing")

    def common(p: argparse.ArgumentParser) -> None:
        target(p)
        p.add_argument(
            "--bad-signature", action="store_true", help="sign with the wrong key (expect 403)"
        )

    for name, text in (
        ("new", "farm_in.new: a new booking request"),
        ("update", "farm_in.update: the requester changes the trip (needs --tid)"),
        ("cancel", "farm_in.update with status CANCEL (needs --tid)"),
        ("quote", "farm_in.quote: price only, persists nothing"),
    ):
        p = sub.add_parser(name, help=text)
        common(p)
        p.add_argument(
            "--tid",
            required=name in ("update", "cancel"),
            default=f"e2e-{uuid.uuid4()}",
            help="transaction id (the idempotency key); reuse it to replay",
        )
        p.add_argument(
            "--grid", default="gnettest", help="requester griddID (Company.gnet_grid_id)"
        )
        p.add_argument(
            "--route", choices=sorted(ROUTES), default="la", help="route preset (default la)"
        )
        p.add_argument(
            "--days",
            type=int,
            default=3,
            help="pickup date offset in the route's zone (neg = past)",
        )
        p.add_argument("--time", default="10:30", help="local pickup time HH:MM (default 10:30)")
        p.add_argument(
            "--vehicle",
            default="SEDAN",
            help="GNet preferredVehicleType (e.g. SUV, VAN_12, LIMO, XYZ)",
        )
        p.add_argument("--hourly", action="store_true", help='runType "Hourly" (as directed)')
        p.add_argument("--pax", type=int, default=2, help="passengerCount (default 2)")
        p.add_argument(
            "--no-coords",
            action="store_true",
            help="drop lat/lon (zone from airport, else assumed)",
        )
        p.add_argument("--res-no", default="", help="requesterResNo (default: from the tid)")
        p.add_argument(
            "--provider-id", default="LATtest", help="providerId (APC's griddID on GNet)"
        )
        p.add_argument("--first", default="Alex", help="passenger first name")
        p.add_argument("--last", default="Smith", help="passenger last name")
        p.add_argument("--passenger-email", default="alex.smith@example.com")
        p.add_argument("--sign-name", default="Mr. Smith", help="namesignInstructions")
        p.add_argument("--note", default="Please confirm receipt", help="AFFILIATE note")
        if name == "cancel":
            p.add_argument(
                "--as-new",
                action="store_true",
                help="send the cancel as farm_in.new (flags the row)",
            )
        p.set_defaults(func=cmd_farm_in)

    p = sub.add_parser("probe", help="GET the farm-in endpoint (the gateway's reachability probe)")
    target(p)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("callback", help="a farm-out status callback to /api/gnet/callback")
    common(p)
    p.add_argument(
        "--assignment", type=int, help="Assignment pk: correlates by requesterResNo apc-<pk>"
    )
    p.add_argument("--transaction-id", default="", help="correlate by gnet_transaction_id instead")
    p.add_argument(
        "--status",
        default="CONFIRMED",
        help="CONFIRMED / ACKNOWLEDGED / ASSIGNED / REJECT / CANCEL / FAILED / CLOSE / EN_ROUTE …",
    )
    p.add_argument("--amount", default=None, help='totalAmount as a string, e.g. "180.00"')
    p.add_argument(
        "--notes", default="", help="affiliateReservation.notes message (REJECT/FAILED reason)"
    )
    p.add_argument(
        "--non-utf8", action="store_true", help="send a non-UTF-8 body (APC-138: expect 400)"
    )
    p.set_defaults(func=cmd_callback)

    p = sub.add_parser(
        "grids", help="list griddIDs, farm-in requests and GNet assignments (read-only)"
    )
    p.set_defaults(func=cmd_grids)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
