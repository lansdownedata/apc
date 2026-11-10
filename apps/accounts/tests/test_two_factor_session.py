from dataclasses import replace
from unittest.mock import patch

from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from apps.accounts import two_factor
from apps.accounts.two_factor import COOKIES, Delivery, Pending, Subject

SMS = Delivery("sms", False)


def _request(cookies: dict | None = None):
    request = RequestFactory().get("/")
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    if cookies:
        request.COOKIES.update(cookies)
    return request


def _subject(kind: str = "staff", pk: int = 7) -> Subject:
    return Subject(kind, pk, "+15715550142", "t@example.com", "T", "pbkdf2$hash")


def _cookie_request(subject: Subject, **override):
    response = HttpResponse()
    two_factor.remember(response, subject)
    name = COOKIES[subject.kind][0]
    return _request({override.get("name", name): response.cookies[name].value})


def test_pending_round_trip():
    request = _request()
    two_factor.begin(request, _subject(), next_url="/portal/leads/", delivery=SMS)
    assert two_factor.pending(request, "staff") == Pending(7, "/portal/leads/", "sms", False)
    two_factor.note_delivery(request, "staff", Delivery("email", True))
    got = two_factor.pending(request, "staff")
    assert (got.channel, got.fell_back) == ("email", True)
    two_factor.clear(request, "staff")
    assert two_factor.pending(request, "staff") is None


def test_pending_expires_after_ten_minutes():
    request = _request()
    two_factor.begin(request, _subject(), next_url="/", delivery=SMS)
    later = timezone.now() + timezone.timedelta(seconds=two_factor.PENDING_TTL + 1)
    with patch("django.utils.timezone.now", return_value=later):
        assert two_factor.pending(request, "staff") is None


def test_malformed_pending_is_none():
    request = _request()
    request.session["two_factor_pending_staff"] = {"pk": "x"}
    assert two_factor.pending(request, "staff") is None


def test_staff_and_planner_pending_are_independent():
    request = _request()
    two_factor.begin(request, _subject("staff", 1), next_url="/a/", delivery=SMS)
    two_factor.begin(request, _subject("planner", 2), next_url="/b/", delivery=SMS)
    assert two_factor.pending(request, "staff").pk == 1
    assert two_factor.pending(request, "planner").pk == 2
    two_factor.clear(request, "staff")
    assert two_factor.pending(request, "staff") is None
    assert two_factor.pending(request, "planner").pk == 2


def test_remembered_cookie_is_accepted():
    subject = _subject()
    assert two_factor.is_remembered(_cookie_request(subject), subject)


def test_no_cookie_is_not_remembered():
    assert not two_factor.is_remembered(_request(), _subject())


def test_cookie_rejected_after_password_change():
    subject = _subject()
    request = _cookie_request(subject)
    assert not two_factor.is_remembered(request, replace(subject, password_hash="other"))


def test_cookie_rejected_after_phone_change():
    subject = _subject()
    request = _cookie_request(subject)
    assert not two_factor.is_remembered(request, replace(subject, phone="+15715550199"))


def test_cookie_for_another_account_is_rejected():
    subject = _subject()
    request = _cookie_request(subject)
    assert not two_factor.is_remembered(request, replace(subject, pk=8))


def test_staff_cookie_value_under_planner_name_is_rejected():
    staff = _subject("staff", 7)
    response = HttpResponse()
    two_factor.remember(response, staff)
    value = response.cookies[COOKIES["staff"][0]].value
    request = _request({COOKIES["planner"][0]: value})
    assert not two_factor.is_remembered(request, _subject("planner", 7))


def test_tampered_cookie_is_rejected():
    subject = _subject()
    response = HttpResponse()
    two_factor.remember(response, subject)
    value = response.cookies[COOKIES["staff"][0]].value
    request = _request({COOKIES["staff"][0]: value[:-2] + "xx"})
    assert not two_factor.is_remembered(request, subject)


def test_cookie_older_than_30_days_is_rejected():
    subject = _subject()
    request = _cookie_request(subject)
    later = timezone.now() + timezone.timedelta(seconds=two_factor.REMEMBER_AGE + 60)
    with patch("time.time", return_value=later.timestamp()):
        assert not two_factor.is_remembered(request, subject)


def test_cookie_flags(settings):
    settings.SESSION_COOKIE_SECURE = True
    response = HttpResponse()
    two_factor.remember(response, _subject("planner", 3))
    morsel = response.cookies["apc_planner_device"]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"
    assert morsel["max-age"] == 2592000
    assert morsel["path"] == "/planner/"
    assert morsel["secure"] is True


def test_forget_deletes_on_same_path():
    response = HttpResponse()
    two_factor.forget(response, "planner")
    morsel = response.cookies["apc_planner_device"]
    assert morsel["path"] == "/planner/"
    assert morsel["max-age"] == 0
