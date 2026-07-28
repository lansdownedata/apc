"""Contract for the reservation editor's date/time controls.

The customer-facing booking widget picks dates and times with flatpickr, themed in
`static/css/app.css`. The portal editor used native `<input type=date|time>`, which
renders OS chrome (`mm/dd/yyyy`, `--:-- --`) and ignores the theme. These tests pin
the editor to the same picker, and the two shells to the same flatpickr build, so
the portal can't drift back to native controls or to a different version.
"""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
EDITOR = TEMPLATES / "leads" / "_reservation_editor.html"

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_both_shells_load_the_same_flatpickr_build():
    portal = (TEMPLATES / "base.html").read_text()
    public = (TEMPLATES / "public" / "base_public.html").read_text()
    version = "flatpickr@4.6.13"
    assert version in public, "the public shell is the reference — update this test if it moves"
    assert version in portal, "the portal shell must load the same flatpickr as the public site"
    assert "flatpickr.min.css" in portal and "flatpickr.min.js" in portal


def test_portal_shell_serves_flatpickr_to_authenticated_pages(client, agent):
    client.force_login(agent)
    html = client.get(reverse("dashboard")).content.decode()
    assert "flatpickr.min.js" in html


def test_editor_has_no_native_date_or_time_inputs():
    src = EDITOR.read_text()
    assert 'type="date"' not in src
    assert 'type="time"' not in src


def test_escape_inside_a_picker_leaves_the_editor_open():
    """flatpickr's Escape closes its calendar; the modal must not also take it."""
    assert "fpJustClosed()" in EDITOR.read_text()


def test_editor_overlay_fully_hides_when_closed():
    """One x-show on the container and no transitions.

    With x-show on the children (or x-transition anywhere) Alpine never applies
    `display: none` to the full-screen container, so after closing the editor its
    invisible backdrop swallows the next click anywhere on the page.
    """
    comments = r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}"
    markup = re.sub(comments, "", EDITOR.read_text(), flags=re.S)
    assert markup.count('x-show="editorOpen"') == 1
    assert "x-transition" not in markup


def test_editor_pickers_use_the_shared_flatpickr_hooks(client, agent):
    lead = LeadFactory()
    client.force_login(agent)
    html = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "data-flatpickr" in html
    assert "data-flatpickr-time" in html
