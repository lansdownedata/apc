"""Browser-side validation on the public forms (spec 2026-11-22-public-form-validation).

The behaviour itself runs in the browser; these pin the contract the markup carries:
Pristine loads before app.js, every form opts out of browser bubbles, each field
carries its rules and copy, and server errors land in the slot Pristine reuses.
"""

from django.template.loader import render_to_string
from django.test import Client, override_settings

PRISTINE = "cdn.jsdelivr.net/npm/pristinejs@1.1.0/dist/pristine.min.js"
CALENDLY = "https://calendly.com/allprocharter/quick-chat"


def test_public_pages_load_pristine_before_app_js(db):
    html = Client().get("/").content.decode()
    assert PRISTINE in html
    assert html.index(PRISTINE) < html.index("js/app.js")


@override_settings(CALENDLY_URL=CALENDLY)
def test_schedule_form_is_a_validated_form(db):
    html = Client().get("/").content.decode()
    assert 'id="sched-email"' in html
    assert '<div data-fv-form novalidate x-data="scheduleBooking(' in html
    assert "ring-danger" not in html


@override_settings(CALENDLY_URL=CALENDLY)
def test_schedule_fields_carry_their_rules_and_copy(db):
    html = Client().get("/").content.decode()
    assert 'data-pristine-required-message="We need a valid email for the calendar invite."' in html
    assert 'data-pristine-email-message="Add the rest of your email, like jane@gmail.com."' in html
    assert "data-pristine-phone" in html


def test_field_error_partial_renders_the_shared_slot():
    out = render_to_string("components/field_error.html", {"field_id": "x", "errors": ["Bad."]})
    assert out.strip() == '<p class="pristine-error fv-msg" id="error-x" role="alert">Bad.</p>'
    empty = render_to_string("components/field_error.html", {"field_id": "x", "errors": []})
    assert empty.strip() == ""
