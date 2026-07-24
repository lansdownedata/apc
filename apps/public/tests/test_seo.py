import json
import re

import pytest
from django.urls import reverse


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


def test_home_embeds_booking_widget(client):
    resp = client.get("/")
    assert resp.status_code == 200
    bookings_url = reverse("public:bookings")
    assert f'action="{bookings_url}"'.encode() in resp.content
    assert b'name="pickup_date"' in resp.content


def test_home_title_refined(client):
    resp = client.get("/")
    assert b"Virginia" in resp.content
    assert b"Transportation" in resp.content


@pytest.mark.parametrize(
    "url,phrase",
    [
        ("/about-us/", b"About"),
        ("/fleet/", b"Fleet"),
        ("/contact/", b"Contact"),
        ("/privacy-policy/", b"Privacy"),
    ],
)
def test_content_pages_render_with_title_and_canonical(client, url, phrase):
    resp = client.get(url)
    assert resp.status_code == 200
    assert phrase in resp.content
    assert b'rel="canonical"' in resp.content
    assert b"<title>" in resp.content


def test_contact_page_emits_contactpage_schema(client):
    resp = client.get("/contact/")
    types = {b.get("@type") for b in _jsonld(resp.content)}
    assert "ContactPage" in types
    assert "LocalBusiness" in types


def test_contact_jsonld_valid_with_query_string(client):
    resp = client.get("/contact/?utm_source=x&utm_medium=y")
    assert resp.status_code == 200

    blocks = re.findall(rb'<script type="application/ld\+json">(.*?)</script>', resp.content, re.S)
    assert blocks, "expected at least one JSON-LD script block"

    parsed = [json.loads(b.decode()) for b in blocks]

    contact_page = next(b for b in parsed if b.get("@type") == "ContactPage")
    assert "&amp;" not in contact_page["url"]


@pytest.mark.parametrize(
    "url,service_name",
    [
        ("/services/airport/", "Airport Transportation"),
        ("/services/corporate/", "Corporate Transportation"),
        ("/services/weddings/", "Wedding Transportation"),
        ("/services/personal/", "Personal Transportation"),
    ],
)
def test_service_pages_emit_service_schema(client, url, service_name):
    resp = client.get(url)
    assert resp.status_code == 200
    names = [b.get("name") for b in _jsonld(resp.content) if b.get("@type") == "Service"]
    assert service_name in names


def test_services_hub_renders(client):
    assert client.get("/services/").status_code == 200


def test_reviews_page_renders(client):
    resp = client.get("/reviews/")
    assert resp.status_code == 200
    assert b'rel="canonical"' in resp.content
    assert b"<title>" in resp.content


def test_rates_page_keeps_legacy_slug(client):
    resp = client.get("/all-pro-charter-rates/")
    assert resp.status_code == 200
    assert b'rel="canonical"' in resp.content


def test_rates_page_drops_stale_covid_banner(client):
    resp = client.get("/all-pro-charter-rates/")
    assert b"COVID" not in resp.content


def test_reviews_no_fabricated_rating(client):
    resp = client.get("/reviews/")
    assert resp.status_code == 200

    types = {b.get("@type") for b in _jsonld(resp.content)}
    assert "AggregateRating" not in types
    assert "Review" not in types

    assert b"ratingValue" not in resp.content
    assert b"reviewCount" not in resp.content
    assert b"aggregateRating" not in resp.content
