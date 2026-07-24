def test_sitemap_lists_key_public_urls(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.content
    for path in [
        b"/about-us/",
        b"/fleet/",
        b"/services/airport/",
        b"/blogs/",
        b"/all-pro-charter-rates/",
        b"/2025/03/the-ultimate-guide-to-selecting-the-right-wedding-transportation-for-2025/",
    ]:
        assert path in body


def test_robots_points_at_sitemap(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"Sitemap:" in resp.content
    assert b"Disallow: /portal/" in resp.content
