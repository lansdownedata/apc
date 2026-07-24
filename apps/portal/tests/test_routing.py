# apps/portal/tests/test_routing.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_staff_routes_live_under_portal_prefix():
    assert reverse("dashboard") == "/portal/"
    assert reverse("review_list") == "/portal/reviews/"
    assert reverse("pipeline") == "/portal/pipeline/"


def test_staff_dashboard_requires_login(client):
    resp = client.get("/portal/")
    assert resp.status_code == 302
    assert reverse("login") in resp["Location"]
