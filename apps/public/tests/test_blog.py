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
