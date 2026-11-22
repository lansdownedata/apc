import json
import re

import pytest


def _jsonld(content: bytes) -> list[dict]:
    blocks = re.findall(rb'<script type="application/ld\+json">(.*?)</script>', content, re.S)
    return [json.loads(b.decode()) for b in blocks]


POSTS = [
    "/2025/03/the-ultimate-guide-to-selecting-the-right-wedding-transportation-for-2025/",
    "/2025/03/all-pro-charter-named-2025-the-knot-best-of-weddings-weddingwire-couples-choice-award-winner/",
    "/2023/11/5-reasons-all-pro-charter-is-your-reliable-transportation-choice/",
    "/2022/11/5-tips-to-traveling-this-holiday-season/",
    "/2021/01/covid-19-update-all-pro-charter-is-cdc-compliant/",
    "/2021/01/all-pro-charter-named-winner-in-2021-weddingwire-couples-choice-awards/",
]


def test_blog_index_renders(client):
    resp = client.get("/blogs/")
    assert resp.status_code == 200
    assert b"<title>" in resp.content
    for url in POSTS:
        assert url.encode() in resp.content


def test_blog_index_newest_first(client):
    resp = client.get("/blogs/")
    assert resp.status_code == 200
    content = resp.content

    offsets = {url: content.index(url.encode()) for url in POSTS}

    # Both 2025-03 posts must appear before both 2021-01 posts.
    posts_2025 = [u for u in POSTS if u.startswith("/2025/03/")]
    posts_2021 = [u for u in POSTS if u.startswith("/2021/01/")]
    assert posts_2025 and posts_2021
    for url_2025 in posts_2025:
        for url_2021 in posts_2021:
            assert offsets[url_2025] < offsets[url_2021], (
                f"{url_2025} should appear before {url_2021} (newest-first ordering)"
            )


@pytest.mark.parametrize("url", POSTS)
def test_blog_posts_keep_dated_urls_with_article_schema(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    types = {b.get("@type") for b in _jsonld(resp.content)}
    assert "Article" in types
    assert "BreadcrumbList" in types


@pytest.mark.parametrize("url", POSTS)
def test_blog_posts_have_seo_metadata(url, client):
    resp = client.get(url)
    assert resp.status_code == 200
    assert b"<title>" in resp.content
    assert b'rel="canonical"' in resp.content


def test_covid_post_preserved_not_redirected(client):
    resp = client.get("/2021/01/covid-19-update-all-pro-charter-is-cdc-compliant/")
    assert resp.status_code == 200
    assert b"COVID-19" in resp.content


# The two 2025 posts carried a "Call Us: (202) 424-2600" line from WordPress. The
# client's 2026-08-31 call is that the marketing site offers a call booking instead
# of the number (test_public_shell.test_phone_ctas_are_replaced_by_schedule_a_call).
CALL_US_POSTS = POSTS[:2]


@pytest.mark.parametrize("url", CALL_US_POSTS)
def test_post_offers_a_call_booking_instead_of_a_bare_number(client, settings, url):
    settings.CALENDLY_URL = "https://calendly.com/allprocharter/quick-chat"
    html = client.get(url).content.decode()
    assert "(202) 424-2600" not in html


@pytest.mark.parametrize("url", CALL_US_POSTS)
def test_post_number_is_tappable_when_calendly_is_off(client, settings, url):
    settings.CALENDLY_URL = ""
    html = client.get(url).content.decode()
    shown = html.count("(202) 424-2600")
    tappable = len(re.findall(r'href="tel:\+12024242600"[^>]*>[^<]*\(202\) 424-2600', html))
    assert shown and shown == tappable
