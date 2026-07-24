import json
import re


def _jsonld(content: bytes) -> list[dict]:
    blocks = re.findall(rb'<script type="application/ld\+json">(.*?)</script>', content, re.S)
    return [json.loads(b.decode()) for b in blocks]


def test_home_has_seo_metadata(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<title>" in resp.content
    assert b'name="description"' in resp.content
    assert b'rel="canonical"' in resp.content


def test_home_emits_localbusiness_schema(client):
    resp = client.get("/")
    types = {b.get("@type") for b in _jsonld(resp.content)}
    assert "LocalBusiness" in types
