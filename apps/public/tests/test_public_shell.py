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
    assert "fonts.googleapis.com/css2?family=Fraunces" in html
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
    # fallback badges present when WEDDINGWIRE_WIDGET is unset (default "")
    assert "badge-weddingawards_en_US.png" in html


@override_settings(WEDDINGWIRE_WIDGET='<div id="ww-live-banner"></div>')
def test_awards_banner_uses_live_snippet_when_set(db):
    html = Client().get("/").content.decode()
    assert 'id="ww-live-banner"' in html
