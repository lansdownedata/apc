import html
import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.urls import reverse
from django.utils import timezone

from apps.accounts import two_factor
from apps.accounts.factories import UserFactory
from apps.accounts.models import SignInCode
from apps.integrations import podium

pytestmark = pytest.mark.django_db

PW = "a-long-test-password-1"
NOW = "django.utils.timezone.now"
SMS_CODE = re.compile(r"your sign-in code is (\d{6})\.")
MAIL_CODE = re.compile(r"\b(\d{3}) (\d{3})\b")
DEVICE_COOKIE = two_factor.COOKIES["staff"][0]


@pytest.fixture(autouse=True)
def texts():
    """Every text Podium would have sent, by body. Nothing leaves the machine."""
    bodies: list[str] = []

    def fake(*, identifier: str, body: str, internal: bool = False, **kw) -> dict:
        bodies.append(body)
        return {"data": {"uid": f"m-{len(bodies)}"}}

    with patch("apps.integrations.podium.send_message", side_effect=fake):
        yield bodies


@pytest.fixture
def staff():
    user = UserFactory(
        username="tarrick@allprocharter.com",
        email="tarrick@allprocharter.com",
        first_name="Tarrick",
        phone="+15715550142",
    )
    user.set_password(PW)
    user.save()
    return user


def _password(client, user, next_url: str = ""):
    url = reverse("login") + (f"?next={next_url}" if next_url else "")
    return client.post(url, {"username": user.username, "password": PW})


def _last_text_code(bodies: list[str]) -> str:
    return SMS_CODE.search(bodies[-1]).group(1)


@pytest.fixture
def sent_code(client, staff, texts):
    """Pass the password step and hand back the code that was texted."""

    def go(next_url: str = "") -> str:
        response = _password(client, staff, next_url)
        assert response.status_code == 302 and response.url == reverse("login_code")
        return _last_text_code(texts)

    return go


def _page(response) -> str:
    """The page text as a reader sees it (apostrophes unescaped)."""
    return html.unescape(response.content.decode())


def _signed_in(client) -> bool:
    return "_auth_user_id" in client.session


def test_correct_password_alone_gives_no_session(client, staff):
    r = client.post(reverse("login"), {"username": staff.username, "password": PW})
    assert r.status_code == 302 and r.url == reverse("login_code")
    assert "_auth_user_id" not in client.session
    assert client.get(reverse("dashboard")).status_code == 302


def test_wrong_password_shows_the_login_form_again(client, staff, texts):
    r = client.post(reverse("login"), {"username": staff.username, "password": "nope"})
    assert r.status_code == 200
    assert texts == []
    assert not SignInCode.objects.exists()


def test_code_page_says_where_the_text_went(client, staff, sent_code):
    sent_code()
    page = _page(client.get(reverse("login_code")))
    assert "Enter your sign-in code" in page
    assert "We texted a 6-digit code to the number ending in 0142." in page
    assert 'autocomplete="one-time-code"' in page and 'inputmode="numeric"' in page
    assert "Remember this device for 30 days" in page
    assert "+15715550142" not in page


def test_correct_code_signs_in_and_honours_next(client, staff, sent_code):
    code = sent_code("/portal/leads/")
    r = client.post(reverse("login_code"), {"code": code, "remember": "1"})
    assert r.status_code == 302 and r.url == "/portal/leads/"
    assert int(client.session["_auth_user_id"]) == staff.pk
    assert two_factor.pending(_FakeRequest(client), "staff") is None


def test_code_with_a_space_is_accepted(client, staff, sent_code):
    code = sent_code()
    r = client.post(reverse("login_code"), {"code": f"{code[:3]} {code[3:]}"})
    assert r.status_code == 302 and _signed_in(client)


def test_external_next_is_ignored(client, staff, sent_code):
    code = sent_code("https://evil.test/")
    r = client.post(reverse("login_code"), {"code": code})
    assert r.status_code == 302 and r.url == reverse("dashboard")


def test_wrong_code_shows_error_and_no_session(client, staff, sent_code):
    code = sent_code()
    wrong = "000000" if code != "000000" else "111111"
    r = client.post(reverse("login_code"), {"code": wrong})
    assert r.status_code == 200
    page = _page(r)
    assert "That code isn't right. Check it and try again." in page
    assert 'role="alert"' in page
    assert not _signed_in(client)


def test_five_wrong_codes_lock_it(client, staff, sent_code):
    code = sent_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = client.post(reverse("login_code"), {"code": wrong})
    assert "Too many tries. Request a new code." in _page(r)
    r = client.post(reverse("login_code"), {"code": code})
    assert "Too many tries. Request a new code." in _page(r)
    assert not _signed_in(client)


def test_expired_code_says_so(client, staff, sent_code):
    code = sent_code()
    row = SignInCode.objects.get()
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save(update_fields=["expires_at"])
    r = client.post(reverse("login_code"), {"code": code})
    assert "That code has expired. Request a new one." in _page(r)
    assert not _signed_in(client)


def test_remember_ticked_sets_cookie_and_skips_code_next_time(client, staff, sent_code, texts):
    code = sent_code()
    r = client.post(reverse("login_code"), {"code": code, "remember": "1"})
    cookie = r.cookies[DEVICE_COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    client.logout()
    client.cookies[DEVICE_COOKIE] = cookie.value
    sent = len(texts)
    r = _password(client, staff)
    assert r.status_code == 302 and r.url == reverse("dashboard")
    assert _signed_in(client)
    assert len(texts) == sent


def test_remember_unticked_sets_no_cookie(client, staff, sent_code):
    code = sent_code()
    r = client.post(reverse("login_code"), {"code": code})
    assert DEVICE_COOKIE not in r.cookies
    assert _signed_in(client)


def test_remembered_cookie_dies_with_a_password_change(client, staff, sent_code):
    code = sent_code()
    r = client.post(reverse("login_code"), {"code": code, "remember": "1"})
    client.logout()
    client.cookies[DEVICE_COOKIE] = r.cookies[DEVICE_COOKIE].value
    staff.set_password(PW + "x")
    staff.save()
    r = client.post(reverse("login"), {"username": staff.username, "password": PW + "x"})
    assert r.url == reverse("login_code")
    assert not _signed_in(client)


def test_code_page_without_pending_redirects_to_login(client):
    r = client.get(reverse("login_code"))
    assert r.status_code == 302 and r.url == reverse("login")
    r = client.post(reverse("login_code"), {"code": "123456"})
    assert r.status_code == 302 and r.url == reverse("login")
    r = client.post(reverse("login_code_resend"), {"channel": "sms"})
    assert r.status_code == 302 and r.url == reverse("login")


def test_signed_in_user_on_the_code_page_goes_to_the_dashboard(client, staff):
    client.force_login(staff)
    r = client.get(reverse("login_code"))
    assert r.status_code == 302 and r.url == reverse("dashboard")


def test_pending_older_than_ten_minutes_redirects_to_login(client, staff, texts):
    _password(client, staff)
    code = _last_text_code(texts)
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.post(reverse("login_code"), {"code": code})
    assert r.status_code == 302 and r.url == reverse("login")
    assert not _signed_in(client)


def test_deactivated_mid_flow_is_refused(client, staff, sent_code):
    code = sent_code()
    staff.is_active = False
    staff.save(update_fields=["is_active"])
    r = client.post(reverse("login_code"), {"code": code})
    assert r.status_code == 302 and r.url == reverse("login")
    assert not _signed_in(client)
    assert two_factor.pending(_FakeRequest(client), "staff") is None
    page = _page(client.get(r.url))
    assert re.search(r'role="alert"[^>]*>\s*<span[^>]*>.*?</span>\s*<span>That username', page, re.S)


def test_staff_code_page_is_never_cached(client, staff, sent_code):
    sent_code()
    r = client.get(reverse("login_code"))
    assert "no-cache" in r["Cache-Control"] or "max-age=0" in r["Cache-Control"]
    assert "no-cache" in client.post(reverse("login_code_resend"), {})["Cache-Control"]


def test_a_timed_out_sign_in_says_so(client, staff, sent_code):
    sent_code()
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.get(reverse("login_code"))
        assert r.status_code == 302 and r.url == reverse("login")
        page = _page(client.get(r.url))
    assert "Your sign-in timed out. Enter your password again." in page
    assert 'role="alert"' in page
    assert "timed out" not in _page(client.get(reverse("login")))


def test_a_timed_out_resend_says_so_and_never_started_is_silent(client, staff, sent_code):
    assert "timed out" not in _page(client.get(client.get(reverse("login_code")).url))
    sent_code()
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.post(reverse("login_code_resend"), {"channel": "sms"})
        assert "Your sign-in timed out." in _page(client.get(r.url))


def test_resend_sends_a_new_code_and_reports_the_limit(client, staff, texts):
    _password(client, staff)
    first = _last_text_code(texts)
    r = client.post(reverse("login_code_resend"), {"channel": "sms"}, follow=True)
    assert "You can request another code in 60 seconds." in _page(r)
    assert len(texts) == 1

    later = timezone.now() + timedelta(seconds=61)
    with patch(NOW, return_value=later):
        r = client.post(reverse("login_code_resend"), {"channel": "sms"})
        assert r.status_code == 302 and r.url == reverse("login_code")
        assert len(texts) == 2
        second = _last_text_code(texts)
        if first != second:
            assert client.post(reverse("login_code"), {"code": first}).status_code == 200
        r = client.post(reverse("login_code"), {"code": second})
    assert r.status_code == 302 and _signed_in(client)


def test_email_me_a_code_instead(client, staff, texts, mailoutbox):
    _password(client, staff)
    later = timezone.now() + timedelta(seconds=61)
    with patch(NOW, return_value=later):
        client.post(reverse("login_code_resend"), {"channel": "email"})
        page = _page(client.get(reverse("login_code")))
        assert "We emailed a 6-digit code to t•••@allprocharter.com." in page
        assert len(texts) == 1 and len(mailoutbox) == 1
        code = "".join(MAIL_CODE.search(mailoutbox[0].body).groups())
        r = client.post(reverse("login_code"), {"code": code})
    assert r.status_code == 302 and _signed_in(client)


def test_staff_without_phone_gets_email_code(client, mailoutbox, texts):
    user = UserFactory(username="nophone", email="nobody@allprocharter.com", phone="")
    user.set_password(PW)
    user.save()
    r = _password(client, user)
    assert r.url == reverse("login_code")
    assert texts == [] and len(mailoutbox) == 1
    page = _page(client.get(reverse("login_code")))
    assert "We emailed a 6-digit code to n•••@allprocharter.com." in page
    assert "Email me a code instead" not in page


def test_text_failure_falls_back_to_email_and_says_so(client, staff, mailoutbox):
    with patch(
        "apps.integrations.podium.send_message", side_effect=podium.PodiumAPIError(500, "x")
    ):
        _password(client, staff)
    page = _page(client.get(reverse("login_code")))
    assert "The text didn't go through, so we emailed the code to t•••@allprocharter.com." in page
    assert len(mailoutbox) == 1


def test_nothing_sent_is_reported(client, staff):
    with (
        patch("apps.integrations.podium.send_message", side_effect=TimeoutError()),
        patch("apps.accounts.two_factor.send_html_email", return_value=False),
    ):
        r = _password(client, staff)
    assert r.url == reverse("login_code")
    assert not _signed_in(client)
    page = _page(client.get(reverse("login_code")))
    assert "We couldn't send your code. Try again in a moment." in page


def test_a_quick_second_password_hits_the_resend_limit_without_crashing(client, staff, texts):
    _password(client, staff)
    first = _last_text_code(texts)
    r = _password(client, staff)
    assert r.status_code == 302 and r.url == reverse("login_code")
    assert not _signed_in(client)
    page = _page(client.get(reverse("login_code")))
    assert "You can request another code in 60 seconds." in page
    assert len(texts) == 1
    # The first code is still the live one and still works.
    r = client.post(reverse("login_code"), {"code": first})
    assert r.status_code == 302 and _signed_in(client)


def test_admin_login_redirects_to_portal_login(client):
    r = client.get("/admin/login/?next=/admin/")
    assert r.status_code == 302 and r.url.startswith(reverse("login"))
    assert parse_qs(urlsplit(r.url).query)["next"] == ["/admin/"]


def test_admin_index_signed_out_lands_on_portal_login(client):
    r = client.get("/admin/", follow=True)
    assert r.redirect_chain[-1][0].startswith(reverse("login"))
    assert r.status_code == 200


def test_admin_still_opens_for_a_signed_in_superuser(client):
    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    assert client.get("/admin/").status_code == 200


def test_switch_off_logs_in_with_password_only(client, staff, two_factor_off, texts):
    r = _password(client, staff, "/portal/leads/")
    assert r.status_code == 302 and r.url == "/portal/leads/"
    assert _signed_in(client)
    assert texts == []


def test_deploy_check_fires_when_off_in_prod(settings):
    settings.TWO_FACTOR_ENABLED = False
    settings.DEBUG = False
    from apps.accounts.checks import two_factor_on_in_production

    errors = two_factor_on_in_production(None)
    assert [e.id for e in errors] == ["accounts.E001"]
    with pytest.raises(SystemCheckError, match="accounts.E001"):
        call_command("check", "--deploy", "--tag", "security")


def test_deploy_check_quiet_when_on_or_debug(settings):
    from apps.accounts.checks import two_factor_on_in_production

    settings.DEBUG = False
    settings.TWO_FACTOR_ENABLED = True
    assert two_factor_on_in_production(None) == []
    settings.DEBUG = True
    settings.TWO_FACTOR_ENABLED = False
    assert two_factor_on_in_production(None) == []


def test_code_view_query_count(client, staff, sent_code, django_assert_max_num_queries):
    sent_code()
    with django_assert_max_num_queries(2):
        client.get(reverse("login_code"))


def test_login_view_query_count(client, staff, django_assert_max_num_queries):
    with django_assert_max_num_queries(12):
        _password(client, staff)


class _FakeRequest:
    """Just enough of a request for `two_factor.pending` to read the test client's session."""

    def __init__(self, client) -> None:
        self.session = client.session
