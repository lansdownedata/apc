"""APC-17 — the "Copy to dates…" control in the quote workspace."""

from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.reservations.factories import TransferReservationFactory

ROOT = Path(__file__).resolve().parents[3]
DETAIL = ROOT / "templates" / "leads" / "lead_detail.html"
MODAL = ROOT / "templates" / "leads" / "_copy_dates_modal.html"
APP_JS = ROOT / "static" / "js" / "app.js"

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(client):
    client.force_login(UserFactory())
    return client


def test_the_row_has_a_copy_to_dates_trigger(agent):
    res = TransferReservationFactory()

    html = agent.get(reverse("lead_detail", args=[res.lead.pk])).content.decode()

    assert f"openCopyDates({res.pk}," in html
    assert 'id="_copy_dates_modal"' not in html  # sanity: not a stray literal
    assert reverse("reservation_copy_dates") in html


def test_the_modal_is_included_and_posts_to_the_endpoint():
    assert '{% include "leads/_copy_dates_modal.html" %}' in DETAIL.read_text()
    body = MODAL.read_text()
    assert "reservation_copy_dates" in body
    # multi-date picker, never a native <select> or window.confirm
    assert "<select" not in body
    assert "window.confirm" not in body
    assert 'name="dates"' in body


def test_app_js_wires_the_multi_date_calendar_and_weekly_repeat():
    source = APP_JS.read_text()
    assert "function copyDatesForm" in source
    assert 'mode: "multiple"' in source
    assert "weeklyDates" in source
    assert "resolvedDates" in source
