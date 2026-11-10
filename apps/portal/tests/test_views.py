import re

import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_dashboard_requires_login(client):
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_dashboard_renders_for_authenticated_user(client, agent):
    client.force_login(agent)
    resp = client.get(reverse("dashboard"))
    assert resp.status_code == 200
    assert "portal/dashboard.html" in {t.name for t in resp.templates}


def test_dashboard_reports_pipeline_counts(client, agent):
    LeadFactory(status=Lead.Status.NEW)
    LeadFactory(status=Lead.Status.QUOTED)
    LeadFactory(status=Lead.Status.QUOTED)
    LeadFactory(status=Lead.Status.BOOKED)
    client.force_login(agent)
    resp = client.get(reverse("dashboard"))
    counts = resp.context["counts"]
    assert counts["new"] == 1
    assert counts["quoted"] == 2
    assert counts["booked"] == 1


def test_sidebar_nav_scrolls_inside_itself_so_the_shell_never_overflows(client, agent):
    """CON-14: on a short window the sidebar was taller than the `h-screen overflow-hidden`
    shell, so focusing the inbox composer or a toast scrolled the whole shell up. The nav
    (the flex-1 child) must be allowed to shrink (`min-h-0`) and scroll (`overflow-y-auto`).
    """
    client.force_login(agent)
    html = client.get(reverse("dashboard")).content.decode()
    aside = re.search(r"<aside[^>]*\bsidebar\b.*?</aside>", html, re.S).group(0)
    nav_classes = re.search(r'<nav class="([^"]*)"', aside).group(1).split()
    assert "flex-1" in nav_classes
    assert "min-h-0" in nav_classes
    assert "overflow-y-auto" in nav_classes
