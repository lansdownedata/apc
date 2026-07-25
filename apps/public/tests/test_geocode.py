from unittest.mock import patch

from django.test import Client, override_settings
from django.urls import reverse


def test_geocode_is_public_and_returns_results(db):
    with patch("apps.public.views.autocomplete", return_value=[{"line1": "123 Main St"}]) as m:
        resp = Client().get(reverse("public:geocode"), {"q": "123 Main"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == [{"line1": "123 Main St"}]
    assert body["degraded"] is False
    m.assert_called_once()


@override_settings(LOCATIONIQ_API_KEY="")
def test_geocode_degraded_when_key_missing(db):
    resp = Client().get(reverse("public:geocode"), {"q": "anything"})
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "degraded": True}


def test_geocode_throttles_per_ip(db):
    from apps.public import views

    with patch("apps.public.views.autocomplete", return_value=[]):
        with override_settings(LOCATIONIQ_API_KEY="x"):
            client = Client()
            over = None
            for _ in range(views.GEOCODE_THROTTLE_LIMIT + 1):
                over = client.get(reverse("public:geocode"), {"q": "a"})
    assert over.status_code == 429
