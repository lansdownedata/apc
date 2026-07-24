import pytest

CASES = [
    ("/trips/", "/services/"),
    ("/all-pro-charter-ao/", "/"),
    ("/allprocharter-register/", "/bookings/"),
    ("/category/press-release/", "/blogs/"),
    ("/category/uncategorized/", "/blogs/"),
]


@pytest.mark.parametrize("src,dst", CASES)
def test_legacy_urls_301_to_target(client, src, dst):
    resp = client.get(src)
    assert resp.status_code == 301
    assert resp["Location"] == dst
