"""Check that the domain-restricted LocationIQ public key will load the driver page's map.

    python scripts/check_locationiq_public_key.py
    python scripts/check_locationiq_public_key.py --key pk.xxxx --origin https://leads.allprocharter.com

The driver page (/trip/driver/<token>/) is served with `Referrer-Policy: no-referrer`, so the
browser sends the map requests with an Origin header and NO Referer. A key restricted by HTTP
referrer may therefore refuse them. This asks LocationIQ for the map style (and one tile) four
ways and says which ones it accepts:

    origin only      what the driver page really sends
    referer + origin what a page WITHOUT the no-referrer policy would send
    referer only     a plain link or image load
    no headers       curl; this passing means the key is not restricted at all

Reads LOCATIONIQ_PUBLIC_KEY and PUBLIC_BASE_URL from .env unless given. Never prints the key.
Exit code 0 = the driver page map will load, 1 = it will not, 2 = could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STYLE_URL = "https://tiles.locationiq.com/v3/streets/vector.json?key="
OTHER_ORIGIN = "https://not-your-site.example"


def _env(key: str) -> str:
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed https host)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach LocationIQ: {exc.reason}")


def _tile_url(style: bytes) -> str:
    """One real tile URL from the style, so the check covers tiles as well as the style."""
    try:
        sources = json.loads(style).get("sources", {})
    except ValueError:
        return ""
    for source in sources.values():
        tiles = source.get("tiles")
        if not tiles and source.get("url"):
            # The style points at a TileJSON document that lists the tile URLs.
            status, body = _get(source["url"], {})
            try:
                tiles = json.loads(body).get("tiles") if status == 200 else None
            except ValueError:
                tiles = None
        if tiles:
            return tiles[0].replace("{z}", "10").replace("{x}", "292").replace("{y}", "391")
    return ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--key", default=_env("LOCATIONIQ_PUBLIC_KEY"), help="The public key.")
    p.add_argument(
        "--origin",
        default=_env("PUBLIC_BASE_URL"),
        help="The site the key is restricted to, e.g. https://leads.allprocharter.com",
    )
    args = p.parse_args()
    if not args.key:
        print("No key: set LOCATIONIQ_PUBLIC_KEY in .env or pass --key.", file=sys.stderr)
        return 2
    if not args.origin:
        print("No origin: set PUBLIC_BASE_URL in .env or pass --origin.", file=sys.stderr)
        return 2
    origin = args.origin.rstrip("/")
    cases = [
        ("origin only (the driver page)", {"Origin": origin}),
        ("referer + origin", {"Origin": origin, "Referer": origin + "/"}),
        ("referer only", {"Referer": origin + "/"}),
        ("no headers", {}),
        ("another site's origin", {"Origin": OTHER_ORIGIN, "Referer": OTHER_ORIGIN + "/"}),
    ]
    print(f"Key ••••{args.key[-4:]} against {origin}\n")
    results: dict[str, bool] = {}
    tile = ""
    for label, headers in cases:
        status, body = _get(STYLE_URL + args.key, headers)
        ok = status == 200
        line = f"  {label:<32} style {status}"
        if ok and not tile:
            tile = _tile_url(body)
        if ok and tile:
            tile_status, _ = _get(tile, headers)
            ok = tile_status == 200
            line += f" · tile {tile_status}"
        results[label] = ok
        print(line + ("" if ok else "   REFUSED"))

    page = results["origin only (the driver page)"]
    print()
    if page and not results["another site's origin"]:
        print("PASS: the driver page map will load, and the key is refused from other sites.")
    elif page:
        print("PASS, with a warning: the map will load, but the key also works from another")
        print("site, so it is not restricted. Restrict it to your domain in LocationIQ.")
    elif results["referer + origin"]:
        print("FAIL: LocationIQ checks the Referer, which the driver page does not send.")
        print("The map will fall back to the stop list. Fix: serve the driver page with")
        print("Referrer-Policy: strict-origin (apps/dispatch/driver_link_views.py), or lift")
        print("the referrer restriction on this key.")
    else:
        print("FAIL: the key is refused even with the right Referer. Check the key and the")
        print("domain it is restricted to in the LocationIQ dashboard.")
    return 0 if page else 1


if __name__ == "__main__":
    sys.exit(main())
