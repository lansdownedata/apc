"""Intuit security requirements: no unused HTTP methods, no cached sensitive pages."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory

pytestmark = pytest.mark.django_db


def _cache_control(response) -> set[str]:
    return {part.strip() for part in response.get("Cache-Control", "").split(",") if part}


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.mark.parametrize("method", ["TRACE", "TRACK", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/healthz/", "/", "/portal/"])
def test_unused_methods_are_refused(client, method, path):
    response = client.generic(method, path)

    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD, POST"


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_used_methods_still_work(client, method):
    assert client.generic(method, "/healthz/").status_code == 200


def test_a_signed_in_page_is_never_cached(staff):
    response = staff.get(reverse("dashboard"))

    assert response.status_code == 200
    assert {"no-cache", "no-store"} <= _cache_control(response)


def test_the_login_redirect_is_never_cached(client):
    response = client.get(reverse("dashboard"))

    assert response.status_code == 302
    assert {"no-cache", "no-store"} <= _cache_control(response)


def test_a_token_page_is_never_cached(client):
    response = client.get(reverse("quote_page", args=["a-quote-token"]))

    assert {"no-cache", "no-store"} <= _cache_control(response)


def test_a_marketing_page_keeps_its_default_caching(client):
    response = client.get(reverse("public:home"))

    assert response.status_code == 200
    assert "no-store" not in _cache_control(response)


def test_a_marketing_page_seen_by_signed_in_staff_is_never_cached(staff):
    response = staff.get(reverse("public:home"))

    assert {"no-cache", "no-store"} <= _cache_control(response)
