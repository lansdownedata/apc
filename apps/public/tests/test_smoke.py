from django.urls import reverse


def test_home_url_is_root():
    assert reverse("public:home") == "/"


def test_home_renders_for_anonymous(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"All Pro Charter" in resp.content
