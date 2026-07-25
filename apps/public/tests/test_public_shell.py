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
