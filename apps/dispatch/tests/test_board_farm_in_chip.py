"""APC-136 — the dispatch board's "N GNet requests" chip."""

import re

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.dispatch import farm_in, farm_in_drawer
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload

pytestmark = pytest.mark.django_db


def _arrive(tid: str, desk: str) -> None:
    farm_in.handle_new(sample_payload(), transaction_id=tid, delivery_id="d")
    FarmInRequest.objects.filter(transaction_id=tid).update(desk=desk)


def _board(client) -> str:
    client.force_login(UserFactory())
    resp = client.get(reverse("dispatch_board"))
    assert resp.status_code == 200
    return resp.content.decode()


def test_board_chip_count_matches_drawer_dispatch_filter(client):
    _arrive("c1", FarmInRequest.Desk.DISPATCH)
    _arrive("c2", FarmInRequest.Desk.DISPATCH)
    _arrive("c3", FarmInRequest.Desk.RESERVATIONS)
    page = _board(client)
    assert re.search(r"data-farm-in-chip[^>]*>\s*2 GNet requests", page)
    assert "farm-in-open" in page
    assert len(farm_in_drawer.rows(tab="pending", dispatch_only=True)) == 2


def test_board_has_no_chip_when_none(client):
    _arrive("c4", FarmInRequest.Desk.RESERVATIONS)
    assert "data-farm-in-chip" not in _board(client)
