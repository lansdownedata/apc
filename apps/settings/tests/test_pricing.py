"""The Pricing settings screen — the default cost ratio (spec 2026-09-05 §3.4)."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.reservations.models import PricingConfig

pytestmark = pytest.mark.django_db


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def test_the_singleton_defaults_to_sixty_five_percent():
    cfg = PricingConfig.load()

    assert cfg.pk == 1
    assert cfg.default_cost_ratio_pct == Decimal("65.00")


def test_loading_twice_returns_the_same_row():
    PricingConfig.load()
    PricingConfig.load()

    assert PricingConfig.objects.count() == 1


def test_screen_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))

    resp = client.get(reverse("pricing"))

    assert resp.status_code in (302, 403)


def test_screen_shows_the_current_default(client):
    _owner(client)

    resp = client.get(reverse("pricing"))

    assert resp.status_code == 200
    assert resp.context["form"].instance.pk == 1


def test_saving_updates_the_singleton(client):
    _owner(client)

    resp = client.post(reverse("pricing"), {"default_cost_ratio_pct": "60"})

    assert resp.status_code == 302
    assert PricingConfig.load().default_cost_ratio_pct == Decimal("60.00")
    assert PricingConfig.objects.count() == 1


def test_the_screen_never_calls_the_ratio_a_margin(client):
    """Spec 1.1: labelled "margin", 65 would be applied as `cost / (1 - .65)` and price the
    trip at $2,857 instead of $1,538.50. The word may only appear on the derived figure."""
    _owner(client)

    body = client.get(reverse("pricing")).content.decode().lower()

    assert "vendor keeps" in body
    assert "margin %" not in body
    assert "margin (%)" not in body


def test_the_screen_explains_which_way_the_lever_runs(client):
    """A lower number is a higher price — the one sentence that stops a misread."""
    _owner(client)

    body = client.get(reverse("pricing")).content.decode().lower()

    assert "lower" in body and "higher" in body


def test_the_settings_index_links_to_it(client):
    _owner(client)

    body = client.get(reverse("settings_index")).content.decode()

    assert reverse("pricing") in body
