"""The link preview every shared URL gets (iMessage, Slack, Facebook, WhatsApp).

With no `og:image` declared, scrapers pick a prominent image off the page — which
made the pink Knot/WeddingWire awards banner the face of the whole company in a
text message. These tests pin a brand card instead.

Every public page template overrides `{% block meta %}` wholesale, so the tags
have to live outside that block; the page sweep below is what keeps that true.
"""

import re
from pathlib import Path

import pytest
from django.test import Client

ROOT = Path(__file__).resolve().parents[3]
CARD = ROOT / "static" / "public" / "og" / "og-card.png"

BLOG_POST = "/2023/11/5-reasons-all-pro-charter-is-your-reliable-transportation-choice/"
# One page per meta-block override style: the hero, an interior page, a blog post.
PAGES = ["/", "/fleet/", "/contact/", BLOG_POST]


def _meta(html: str, prop: str) -> str | None:
    match = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', html)
    return match.group(1) if match else None


def test_the_card_asset_is_a_correctly_sized_png():
    from PIL import Image

    assert CARD.exists(), "the shared social card is missing"
    with Image.open(CARD) as im:
        assert im.size == (1200, 630), f"og:image must be 1200x630, got {im.size}"


@pytest.mark.parametrize("path", PAGES)
def test_every_page_declares_the_brand_card(db, path):
    resp = Client().get(path)
    assert resp.status_code == 200, f"{path} did not render"
    image = _meta(resp.content.decode(), "og:image")
    assert image, f"{path} has no og:image — scrapers will pick their own"
    assert image.startswith("http://testserver/"), f"og:image must be absolute: {image}"
    assert image.endswith("/og-card.png"), f"{path} points somewhere else: {image}"


@pytest.mark.parametrize("path", PAGES)
def test_every_page_asks_for_the_large_card(db, path):
    html = Client().get(path).content.decode()
    assert _meta(html, "twitter:card") == "summary_large_image"
    assert _meta(html, "og:image:width") == "1200"
    assert _meta(html, "og:image:height") == "630"
    assert _meta(html, "og:url"), "og:url missing — previews fall back to the raw link"
