"""Planner sign-in with the texted code: login, registration and reset all end on the code
page, and a staff session in the same browser is never touched."""

import html
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import two_factor
from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.planners import services
from apps.planners.auth import SESSION_KEY
from apps.planners.models import PlannerAccount
from apps.planners.tokens import planner_reset_token, planner_token
from apps.planners.views import LINK_SESSION_KEY

pytestmark = pytest.mark.django_db

PW = "a-long-pass-phrase-77"
NEW_PW = "another-long-phrase-88"
NOW = "django.utils.timezone.now"
SEND = "apps.notifications.email.deliver_html_email"
SMS_CODE = re.compile(r"your sign-in code is (\d{6})\.")
MAIL_CODE = re.compile(r"\b(\d{3}) (\d{3})\b")
PLANNER_COOKIE, PLANNER_PATH = two_factor.COOKIES["planner"]
STAFF_COOKIE = two_factor.COOKIES["staff"][0]


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
def invited_planner() -> PlannerAccount:
    contact = ContactFactory(name="Dana Planner", email="dana@example.com", phone="(571) 555-0142")
    with patch(SEND, return_value="pm-1"):
        account, _ = services.invite(contact, actor=UserFactory())
    return account


@pytest.fixture
def planner(invited_planner) -> PlannerAccount:
    return services.register(invited_planner, PW)


@pytest.fixture
def staff():
    user = UserFactory(
        username="tarrick@allprocharter.com",
        email="tarrick@allprocharter.com",
        first_name="Tarrick",
        phone="+15715550199",
    )
    user.set_password(PW)
    user.save()
    return user


@pytest.fixture
def sent_code(texts):
    """The code the latest text carried."""
    return lambda: SMS_CODE.search(texts[-1]).group(1)


def _password(client, email: str = "dana@example.com", password: str = PW, next_url: str = ""):
    data = {"email": email, "password": password}
    if next_url:
        data["next"] = next_url
    return client.post(reverse("planners:login"), data)


def _link(account: PlannerAccount, name: str, generator) -> str:
    """A registration (`register`) or reset (`set_password`) link as the email carries it."""
    return reverse(
        f"planners:{name}",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": generator.make_token(account),
        },
    )


def _choose_password(client, account, name: str, generator, password: str):
    """Follow the link (the token moves into the session) and choose a password."""
    account.refresh_from_db()  # the token hashes last_login, which a sign-in moves
    hop = client.get(_link(account, name, generator))
    assert hop.status_code == 302
    return client.post(hop.url, {"new_password1": password, "new_password2": password})


def _page(response) -> str:
    return html.unescape(response.content.decode())


def _planner_in(client) -> bool:
    return SESSION_KEY in client.session


def _pending(client):
    return two_factor.pending(_FakeRequest(client), "planner")


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_planner_password_alone_gives_no_session(client, planner):
    r = _password(client)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert SESSION_KEY not in client.session
    assert client.get(reverse("planners:weddings")).status_code == 302


def test_wrong_password_sends_nothing(client, planner, texts):
    r = _password(client, password="not-the-password-1")
    assert r.status_code == 200
    assert texts == []
    assert _pending(client) is None


def test_code_page_says_where_the_text_went(client, planner, sent_code):
    _password(client)
    r = client.get(reverse("planners:login_code"))
    page = _page(r)
    assert "Enter your sign-in code" in page
    assert "We texted a 6-digit code to the number ending in 0142." in page
    assert 'autocomplete="one-time-code"' in page and 'inputmode="numeric"' in page
    assert "Remember this device for 30 days" in page
    assert "Email me a code instead" in page
    assert "+15715550142" not in page
    assert "no-cache" in r["Cache-Control"] or "max-age=0" in r["Cache-Control"]


def test_planner_correct_code_signs_in(client, planner, sent_code):
    _password(client, next_url="/planner/weddings/7/")
    r = client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert r.status_code == 302 and r.url == "/planner/weddings/7/"
    assert client.session[SESSION_KEY] == planner.pk
    assert _pending(client) is None
    assert client.get(reverse("planners:weddings")).status_code == 200


def test_correct_code_without_next_lands_on_weddings(client, planner, sent_code):
    _password(client)
    code = sent_code()
    r = client.post(reverse("planners:login_code"), {"code": f"{code[:3]} {code[3:]}"})
    assert r.status_code == 302 and r.url == reverse("planners:weddings")


def test_external_next_is_ignored(client, planner, sent_code):
    _password(client, next_url="https://evil.test/")
    r = client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert r.status_code == 302 and r.url == reverse("planners:weddings")


def test_wrong_code_shows_an_alert_and_no_session(client, planner, sent_code):
    _password(client)
    r = client.post(reverse("planners:login_code"), {"code": _wrong(sent_code())})
    assert r.status_code == 200
    page = _page(r)
    assert "That code isn't right. Check it and try again." in page
    assert 'role="alert"' in page
    assert not _planner_in(client)


def test_five_wrong_codes_lock_it(client, planner, sent_code):
    _password(client)
    code = sent_code()
    for _ in range(5):
        r = client.post(reverse("planners:login_code"), {"code": _wrong(code)})
    assert "Too many tries. Request a new code." in _page(r)
    r = client.post(reverse("planners:login_code"), {"code": code})
    assert "Too many tries. Request a new code." in _page(r)
    assert not _planner_in(client)


def test_code_page_without_pending_redirects_to_login(client):
    for r in (
        client.get(reverse("planners:login_code")),
        client.post(reverse("planners:login_code"), {"code": "123456"}),
        client.post(reverse("planners:login_code_resend"), {"channel": "sms"}),
    ):
        assert r.status_code == 302 and r.url == reverse("planners:login")


def test_pending_older_than_ten_minutes_redirects_to_login(client, planner, sent_code):
    _password(client)
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert r.status_code == 302 and r.url == reverse("planners:login")
    assert not _planner_in(client)


def test_registration_goes_through_the_code_page(client, invited_planner, sent_code):
    r = _choose_password(client, invited_planner, "register", planner_token, PW)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert not _planner_in(client)
    assert LINK_SESSION_KEY not in client.session
    page = _page(client.get(reverse("planners:login_code")))
    assert "You're all set." not in page

    r = client.post(reverse("planners:login_code"), {"code": sent_code()}, follow=True)
    assert r.redirect_chain[-1][0] == reverse("planners:weddings")
    assert client.session[SESSION_KEY] == invited_planner.pk
    assert "You're all set." in _page(r)


def test_password_reset_goes_through_the_code_page(client, planner, sent_code):
    r = _choose_password(client, planner, "set_password", planner_reset_token, NEW_PW)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert not _planner_in(client)
    assert LINK_SESSION_KEY not in client.session

    r = client.post(reverse("planners:login_code"), {"code": sent_code()}, follow=True)
    assert r.redirect_chain[-1][0] == reverse("planners:weddings")
    assert client.session[SESSION_KEY] == planner.pk
    assert "Password updated." in _page(r)


def _remembered_cookie(client, sent_code) -> str:
    _password(client)
    r = client.post(reverse("planners:login_code"), {"code": sent_code(), "remember": "1"})
    value = r.cookies[PLANNER_COOKIE].value
    client.post(reverse("planners:logout"))
    assert not _planner_in(client)
    return value


def test_remembered_planner_device_skips_the_code(client, planner, sent_code, texts):
    cookie = _remembered_cookie(client, sent_code)
    client.cookies[PLANNER_COOKIE] = cookie
    sent = len(texts)
    r = _password(client, next_url="/planner/weddings/7/")
    assert r.status_code == 302 and r.url == "/planner/weddings/7/"
    assert client.session[SESSION_KEY] == planner.pk
    assert len(texts) == sent


def test_password_reset_ends_remembered_devices(client, planner, sent_code, texts):
    cookie = _remembered_cookie(client, sent_code)
    client.cookies[PLANNER_COOKIE] = cookie
    later = timezone.now() + timedelta(seconds=61)
    with patch(NOW, return_value=later):
        r = _choose_password(client, planner, "set_password", planner_reset_token, NEW_PW)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert not _planner_in(client)

    client.cookies[PLANNER_COOKIE] = cookie
    later = timezone.now() + timedelta(seconds=122)
    with patch(NOW, return_value=later):
        r = _password(client, password=NEW_PW)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert not _planner_in(client)


def test_revoked_mid_flow_is_refused(client, planner, sent_code):
    _password(client)
    services.revoke(planner, actor=UserFactory())
    r = client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert r.status_code == 200
    page = _page(r)
    assert 'role="alert"' in page
    assert "That email and password don't match." in page
    assert not _planner_in(client)
    assert _pending(client) is None


def test_staff_code_is_useless_on_the_planner_page(client, planner, staff):
    staff_code, planner_code = 123456, 654321
    with patch("apps.accounts.two_factor.secrets.randbelow", side_effect=[staff_code]):
        client.post(reverse("login"), {"username": staff.username, "password": PW})
    with patch("apps.accounts.two_factor.secrets.randbelow", side_effect=[planner_code]):
        _password(client)
    r = client.post(reverse("planners:login_code"), {"code": str(staff_code)})
    assert "That code isn't right. Check it and try again." in _page(r)
    assert not _planner_in(client)
    assert "_auth_user_id" not in client.session


def test_planner_code_step_leaves_staff_session_alone(client, planner, staff, sent_code):
    client.force_login(staff)
    client.post(reverse("planners:login"), {"email": planner.email, "password": PW})
    assert client.get(reverse("dashboard")).status_code == 200  # staff still in
    assert client.get(reverse("planners:weddings")).status_code == 302  # planner not yet
    client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert client.get(reverse("dashboard")).status_code == 200
    assert client.get(reverse("planners:weddings")).status_code == 200


def test_planner_cookie_is_scoped_to_the_planner_path(client, planner, sent_code):
    _password(client)
    r = client.post(reverse("planners:login_code"), {"code": sent_code(), "remember": "1"})
    cookie = r.cookies[PLANNER_COOKIE]
    assert PLANNER_PATH == "/planner/" and cookie["path"] == "/planner/"
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert STAFF_COOKIE not in r.cookies


def test_remember_unticked_sets_no_cookie(client, planner, sent_code):
    _password(client)
    r = client.post(reverse("planners:login_code"), {"code": sent_code()})
    assert PLANNER_COOKIE not in r.cookies
    assert _planner_in(client)


def test_code_post_is_throttled_per_ip(client, planner, sent_code):
    _password(client)
    code = sent_code()
    for _ in range(10):
        r = client.post(reverse("planners:login_code"), {"code": _wrong(code)})
        assert r.status_code == 200
    r = client.post(reverse("planners:login_code"), {"code": code})
    assert r.status_code == 429
    page = _page(r)
    assert 'role="alert"' in page and "Too many attempts." in page
    assert not _planner_in(client)


def test_resend_sends_a_new_code_and_reports_the_limit(client, planner, sent_code, texts):
    _password(client)
    first = sent_code()
    r = client.post(reverse("planners:login_code_resend"), {"channel": "sms"}, follow=True)
    assert "You can request another code in 60 seconds." in _page(r)
    assert len(texts) == 1

    later = timezone.now() + timedelta(seconds=61)
    with patch(NOW, return_value=later):
        r = client.post(reverse("planners:login_code_resend"), {"channel": "sms"})
        assert r.status_code == 302 and r.url == reverse("planners:login_code")
        assert len(texts) == 2
        second = sent_code()
        if first != second:
            assert client.post(reverse("planners:login_code"), {"code": first}).status_code == 200
        r = client.post(reverse("planners:login_code"), {"code": second})
    assert r.status_code == 302 and _planner_in(client)


def test_email_me_a_code_instead(client, planner, texts, mailoutbox):
    _password(client)
    later = timezone.now() + timedelta(seconds=61)
    with patch(NOW, return_value=later):
        client.post(reverse("planners:login_code_resend"), {"channel": "email"})
        page = _page(client.get(reverse("planners:login_code")))
        assert "We emailed a 6-digit code to d•••@example.com." in page
        code = "".join(MAIL_CODE.search(mailoutbox[-1].body).groups())
        r = client.post(reverse("planners:login_code"), {"code": code})
    assert r.status_code == 302 and _planner_in(client)


def test_planner_without_a_phone_gets_the_code_by_email(client, planner, texts, mailoutbox):
    PlannerAccount.objects.filter(pk=planner.pk).update(phone="")
    r = _password(client)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    assert texts == [] and len(mailoutbox) == 1
    page = _page(client.get(reverse("planners:login_code")))
    assert "We emailed a 6-digit code to d•••@example.com." in page
    assert "Email me a code instead" not in page
    code = "".join(MAIL_CODE.search(mailoutbox[0].body).groups())
    r = client.post(reverse("planners:login_code"), {"code": code})
    assert r.status_code == 302 and _planner_in(client)


def test_a_quick_second_password_hits_the_resend_limit_without_crashing(
    client, planner, sent_code, texts
):
    _password(client)
    first = sent_code()
    r = _password(client)
    assert r.status_code == 302 and r.url == reverse("planners:login_code")
    page = _page(client.get(reverse("planners:login_code")))
    assert "You can request another code in 60 seconds." in page
    assert len(texts) == 1
    r = client.post(reverse("planners:login_code"), {"code": first})
    assert r.status_code == 302 and _planner_in(client)


def test_switch_off_signs_planner_in_with_password_only(client, planner, two_factor_off, texts):
    r = _password(client, next_url="/planner/weddings/7/")
    assert r.status_code == 302 and r.url == "/planner/weddings/7/"
    assert client.session[SESSION_KEY] == planner.pk
    assert texts == []


def test_switch_off_registration_signs_in_with_the_flash(
    client, invited_planner, two_factor_off, texts
):
    r = _choose_password(client, invited_planner, "register", planner_token, PW)
    assert r.status_code == 302 and r.url == reverse("planners:weddings")
    assert client.session[SESSION_KEY] == invited_planner.pk
    assert "You're all set." in _page(client.get(r.url))
    assert texts == []


class _FakeRequest:
    """Just enough of a request for `two_factor.pending` to read the test client's session."""

    def __init__(self, client) -> None:
        self.session = client.session


def test_a_timed_out_planner_sign_in_says_so(client, planner, sent_code):
    _password(client)
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.get(reverse("planners:login_code"))
        assert r.status_code == 302 and r.url == reverse("planners:login")
        page = _page(client.get(r.url))
    assert re.search(r'role="alert"[^>]*>\s*Your sign-in timed out\. Enter your password again\.', page)
    assert "timed out" not in _page(client.get(reverse("planners:login")))


def test_a_timed_out_planner_resend_says_so_and_never_started_is_silent(client, planner, sent_code):
    assert "timed out" not in _page(client.get(client.get(reverse("planners:login_code")).url))
    _password(client)
    later = timezone.now() + timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch(NOW, return_value=later):
        r = client.post(reverse("planners:login_code_resend"), {"channel": "sms"})
        assert "Your sign-in timed out." in _page(client.get(r.url))
