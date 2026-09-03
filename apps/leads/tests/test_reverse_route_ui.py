"""APC-16 — the quote workspace's Reverse Route control.

Server-rendered markup, so this asserts the rendered HTML: a hidden POST form to
`reservation_reverse` and a button that gates it on the shared `$store.modal` confirm
(never `window.confirm`).
"""

from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.reservations import groups
from apps.reservations.factories import TransferReservationFactory

ROOT = Path(__file__).resolve().parents[3]
DETAIL = ROOT / "templates" / "leads" / "lead_detail.html"

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())
    return client


def _html(client, lead) -> str:
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


def test_the_row_offers_a_reverse_route_form_posting_to_the_endpoint(agent):
    res = TransferReservationFactory()

    html = _html(agent, res.lead)

    assert reverse("reservation_reverse", args=[res.pk]) in html
    assert f"form-rev-{res.pk}" in html


def test_reverse_is_gated_on_the_shared_modal_not_window_confirm():
    source = DETAIL.read_text()
    block = source[source.index("form-rev-") :]
    block = block[: block.index("</button>")]
    assert "$store.modal.confirm" in block
    assert "window.confirm" not in block


def test_a_linked_set_reverses_as_one(agent):
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)

    html = _html(agent, res.lead)

    # one reverse form per line (the anchor), not one per member
    assert html.count('id="form-rev-') == 1
