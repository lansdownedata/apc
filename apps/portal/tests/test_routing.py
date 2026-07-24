# apps/portal/tests/test_routing.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_staff_routes_live_under_app_prefix():
    assert reverse("dashboard") == "/app/"
    assert reverse("review_list") == "/app/reviews/"
    assert reverse("pipeline") == "/app/pipeline/"


def test_staff_dashboard_requires_login(client):
    resp = client.get("/app/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]
