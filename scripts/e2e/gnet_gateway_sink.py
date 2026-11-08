"""A local stand-in for the Lansdowne GNet gateway, so outbound GNet calls never leave the laptop.

There is no GNet sandbox: `GNET_GATEWAY_URL` defaults to https://lansdownedata.com, the LIVE
gateway in front of real GNet, where a farm-out books a real vehicle with a real operator.
This sink answers the three calls the portal makes (`apps/integrations/gnet.py`) and logs
every request so you can read the exact payload:

  POST   /api/gateway/v1/trips             -> {"transactionId": "sink-<uuid>"}  farm-out send
  DELETE /api/gateway/v1/trips/<id>        -> {"success": true}                 farm-out cancel
  POST   /api/gateway/v1/trips/<id>/status -> {"success": true}                 farm-in push

Use it by putting these in .env and restarting runserver (and remove them afterwards):
  GNET_GATEWAY_URL=http://127.0.0.1:8765
  GNET_API_KEY=e2e-local-dummy           # arms farm-in Accept / status pushes
  GNET_ACTIVE=True                       # ONLY with the URL above: arms farm-out sends

--fail CODE answers every call with that HTTP status instead (503/409 retry with backoff on
the farm-in push queue; 422 fails at once; on farm-out any failure is an ERROR, never retried).

Examples (repo root, venv active):
  python scripts/e2e/gnet_gateway_sink.py
  python scripts/e2e/gnet_gateway_sink.py --port 8765 --fail 503
  python scripts/e2e/gnet_gateway_sink.py --check        # only report what .env points at
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _bootstrap import BASE_URL, django_ready, env  # noqa: F401  (no DB use; parity with siblings)

API = "/api/gateway/v1/trips"


def check_env(port: int) -> bool:
    """Say loudly whether .env sends GNet calls here or to the live gateway."""
    url = env("GNET_GATEWAY_URL", "https://lansdownedata.com")
    key = env("GNET_API_KEY")
    active = env("GNET_ACTIVE").lower() in ("1", "true", "yes", "on")
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    print(f".env GNET_GATEWAY_URL = {url}")
    print(f".env GNET_API_KEY     = {'set' if key else 'blank'}")
    print(f".env GNET_ACTIVE      = {active}")
    if (key or active) and not local:
        print(
            "\n*** DANGER: GNET_API_KEY/GNET_ACTIVE are set but GNET_GATEWAY_URL is NOT local.\n"
            "*** Farm-in pushes / farm-out sends would hit the LIVE gateway. Fix .env first."
        )
        return False
    if local and f":{port}" not in url:
        print(f"\nNote: .env points at {url}, but this sink listens on port {port}.")
    return True


class Handler(BaseHTTPRequestHandler):
    fail_code: int = 0

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _log_request(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        stamp = datetime.now().strftime("%H:%M:%S")
        auth = self.headers.get("Authorization", "")
        print(f"\n[{stamp}] {self.command} {self.path}   auth={'Bearer …' if auth else 'none'}")
        if body:
            try:
                print("  " + json.dumps(json.loads(body), indent=2).replace("\n", "\n  "))
            except ValueError:
                print(f"  {body!r}")
        return body

    def _route(self) -> None:
        self._log_request()
        if self.fail_code:
            print(f"  -> {self.fail_code} (--fail)")
            self._reply(self.fail_code, {"error": f"sink forced {self.fail_code}"})
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if self.command == "POST" and path == API:
            tid = f"sink-{uuid.uuid4()}"
            print(f"  -> 200 transactionId={tid}")
            self._reply(200, {"transactionId": tid})
        elif self.command == "POST" and path.startswith(API + "/") and path.endswith("/status"):
            print("  -> 200 success")
            self._reply(200, {"success": True})
        elif self.command == "DELETE" and path.startswith(API + "/"):
            print("  -> 200 success")
            self._reply(200, {"success": True})
        else:
            print("  -> 404")
            self._reply(404, {"error": "sink: unknown route"})

    do_POST = _route
    do_DELETE = _route
    do_GET = _route

    def log_message(self, *args) -> None:  # silence the default access log; we print our own
        return


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local fake of the Lansdowne GNet gateway (logs requests, never forwards).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Use it by", 1)[1].join(["Use it by", ""]),
    )
    parser.add_argument("--port", type=int, default=8765, help="listen port (default 8765)")
    parser.add_argument("--fail", type=int, default=0, help="answer every call with this status")
    parser.add_argument("--check", action="store_true", help="only report .env, do not listen")
    args = parser.parse_args()

    safe = check_env(args.port)
    if args.check:
        return 0 if safe else 1
    Handler.fail_code = args.fail
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"\nGNet gateway sink on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
