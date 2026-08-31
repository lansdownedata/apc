from django.test import Client, override_settings


@override_settings(CALENDLY_URL="https://calendly.com/allprocharter/quick-chat")
def test_calendly_url_exposed_to_public_templates(db):
    resp = Client().get("/")
    assert resp.status_code == 200
    assert b"calendly.com/allprocharter/quick-chat" in resp.content


@override_settings(WEDDINGWIRE_WIDGET='<div id="ww-live">LIVE</div>')
def test_weddingwire_snippet_rendered_when_set(db):
    resp = Client().get("/")
    assert b'id="ww-live"' in resp.content


def test_public_shell_loads_foundation(db):
    html = Client().get("/").content.decode()
    # which faces load is owned by apps/core/tests/test_typography.py — this only
    # asserts the shell pulls a webfont stylesheet at all
    assert "fonts.googleapis.com/css2?family=" in html
    assert "@tabler/icons-webfont" in html
    assert "intlTelInput" in html
    assert "flatpickr" in html
    assert "alpinejs" in html
    # shared modal store markup present so no page needs a native dialog
    assert "$store.modal.open" in html or "$store.modal" in html


def test_review_cards_bottom_align_names(db):
    html = Client().get("/").content.decode()
    # each review figure is a flex column and pushes the figcaption down
    assert "flex flex-col" in html
    assert "mt-auto" in html


@override_settings(CALENDLY_URL="https://calendly.com/allprocharter/quick-chat")
def test_calendly_popup_button_and_assets(db):
    html = Client().get("/").content.decode()
    assert "assets.calendly.com/assets/external/widget.js" in html
    assert "Calendly.initPopupWidget" in html
    assert "Schedule a call" in html


def test_awards_banner_static_fallback_when_no_snippet(db):
    html = Client().get("/").content.decode()
    # client's own 2025 award banner shows when WEDDINGWIRE_WIDGET is unset (default ""),
    # and the graphic's baked-in "Book Now" links to the booking page.
    assert "awards-banner-2025.webp" in html
    assert 'href="/bookings/"' in html


@override_settings(WEDDINGWIRE_WIDGET='<div id="ww-live-banner"></div>')
def test_awards_banner_uses_live_snippet_when_set(db):
    html = Client().get("/").content.decode()
    assert 'id="ww-live-banner"' in html


CAL = "https://calendly.com/allprocharter/quick-chat"

# A representative sweep: a landing page, a service page, a legacy-slug page, the
# contact page, the blog index and a blog post. Every one of them carried a
# "Call (202) 424-2600" CTA before the Calendly swap.
MARKETING_PAGES = (
    "/",
    "/all-pro-charter-rates/",
    "/about-us/",
    "/fleet/",
    "/reviews/",
    "/services/",
    "/services/airport/",
    "/contact/",
    "/blogs/",
    "/2023/11/5-reasons-all-pro-charter-is-your-reliable-transportation-choice/",
)


@override_settings(CALENDLY_URL=CAL)
def test_calendly_assets_load_on_every_public_page_not_just_home(db):
    """The button now lives on more than one page, so the assets moved to the shell."""
    html = Client().get("/contact/").content.decode()
    assert "assets.calendly.com/assets/external/widget.js" in html


@override_settings(CALENDLY_URL=CAL)
def test_calendly_popup_redirects_the_parent_window_after_booking(db):
    html = Client().get("/").content.decode()
    assert "calendly.event_scheduled" in html
    assert "/schedule/thanks/" in html
    # The listener runs on every page and any frame can postMessage — origin is checked.
    assert 'https://calendly.com"' in html


@override_settings(CALENDLY_URL=CAL)
def test_calendly_assets_are_included_exactly_once(db):
    """They used to live in home.html's body_extra; the shell owns them now."""
    html = Client().get("/").content.decode()
    assert html.count("assets/external/widget.js") == 1


@override_settings(CALENDLY_URL="")
def test_no_calendly_anything_when_unset(db):
    for path in ("/", "/contact/"):
        html = Client().get(path).content.decode()
        assert "assets.calendly.com" not in html
        assert "Calendly.initPopupWidget" not in html


@override_settings(CALENDLY_URL=CAL)
def test_phone_ctas_are_replaced_by_schedule_a_call(db):
    """Client's call, 2026-08-31: booking a slot replaces dialling us everywhere on
    the marketing site. schema.org, the throttle message and the portal keep the
    number — see test_schema_org_keeps_the_phone_number."""
    for path in MARKETING_PAGES:
        html = Client().get(path).content.decode()
        assert 'href="tel:' not in html, f"stray tel: link on {path}"
        assert "Schedule a call" in html, f"no Calendly CTA on {path}"


@override_settings(CALENDLY_URL="")
def test_phone_ctas_come_back_when_calendly_is_switched_off(db):
    """Blanking CALENDLY_URL must not leave a page with no way to reach the company."""
    for path in MARKETING_PAGES:
        html = Client().get(path).content.decode()
        assert 'href="tel:+12024242600"' in html, f"no fallback on {path}"


@override_settings(CALENDLY_URL=CAL)
def test_schema_org_keeps_the_phone_number(db):
    """LocalBusiness `telephone` feeds Google's knowledge panel and local pack. It is
    machine-read, never displayed, so the swap must not touch it."""
    html = Client().get("/").content.decode()
    assert '"telephone": "+1-202-424-2600"' in html
