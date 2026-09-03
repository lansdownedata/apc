"""APC-15 — Create Return Trip in the quote workspace.

Server-rendered markup + the JS wiring asserted by reading `static/js/app.js` as a file
(same approach as the other workspace UI tests — no JS harness in this repo).
"""

from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.reservations import routing
from apps.reservations.factories import TransferReservationFactory

ROOT = Path(__file__).resolve().parents[3]
DETAIL = ROOT / "templates" / "leads" / "lead_detail.html"
APP_JS = ROOT / "static" / "js" / "app.js"

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())
    return client


def _html(client, lead) -> str:
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


def test_the_row_offers_a_create_return_trip_form(agent):
    res = TransferReservationFactory()

    html = _html(agent, res.lead)

    assert reverse("reservation_return", args=[res.pk]) in html
    assert f"form-ret-{res.pk}" in html


def test_edit_query_param_opens_the_editor_on_that_trip(agent):
    res = TransferReservationFactory()
    ret = routing.create_return_trip(res)

    url = reverse("lead_detail", args=[res.lead_id])
    html = agent.get(f"{url}?edit={ret.pk}").content.decode()

    assert f"openEditorId: {ret.pk}" in html


def test_edit_query_param_for_a_foreign_trip_is_ignored(agent):
    res = TransferReservationFactory()
    other = TransferReservationFactory()  # different lead

    url = reverse("lead_detail", args=[res.lead_id])
    html = agent.get(f"{url}?edit={other.pk}").content.decode()

    assert "openEditorId: null" in html


def test_app_js_reopens_the_editor_from_open_editor_id():
    source = APP_JS.read_text()
    start = source.find("openEditorId: opts.openEditorId")
    assert start != -1
    init = source.find("init()", start)
    assert "this.editReservation(Number(this.openEditorId))" in source[init : init + 600]
