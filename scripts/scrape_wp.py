# scripts/scrape_wp.py — one-time WordPress content extractor. Not part of the app.
"""Pull pages, posts, SEO <head>, and images from allprocharter.com into docs/migration/."""
import json
import pathlib
import re
import urllib.error
import urllib.request

BASE = "https://allprocharter.com"
OUT = pathlib.Path("docs/migration/inventory")
HEAD = OUT / "_head"
IMG = pathlib.Path("static/public/img")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "apc-migration/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _wp(endpoint: str) -> list[dict]:
    out, page = [], 1
    while True:
        try:
            raw = _get(f"{BASE}/wp-json/wp/v2/{endpoint}?per_page=100&page={page}")
        except urllib.error.HTTPError as e:
            # WP REST API returns 400 rest_post_invalid_page_number once `page`
            # exceeds the available pages, instead of an empty list.
            if e.code == 400:
                break
            raise
        batch = json.loads(raw)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        page += 1
    return out


def run() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    HEAD.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    for kind in ("pages", "posts"):
        for item in _wp(kind):
            slug = item["slug"]
            (OUT / f"{slug}.json").write_text(json.dumps({
                "slug": slug,
                "link": item["link"],
                "title": item["title"]["rendered"],
                "content_html": item["content"]["rendered"],
                "excerpt": item["excerpt"]["rendered"],
                "date": item.get("date"),
            }, indent=2))
            # SEO head snapshot (Yoast title/description + JSON-LD)
            html = _get(item["link"]).decode("utf-8", "replace")
            (HEAD / f"{slug}.json").write_text(json.dumps({
                "title_tag": _first(r"<title>(.*?)</title>", html),
                "meta_description": _first(
                    r'<meta name="description" content="(.*?)"', html),
                "canonical": _first(r'<link rel="canonical" href="(.*?)"', html),
                "jsonld": re.findall(
                    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S),
                "images": re.findall(r'<img[^>]+src="([^"]+)"[^>]*alt="([^"]*)"', html),
            }, indent=2))
    print(f"wrote inventory to {OUT}")


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else ""


if __name__ == "__main__":
    run()
