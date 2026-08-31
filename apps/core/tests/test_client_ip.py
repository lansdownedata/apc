"""Identifying the visitor behind a reverse proxy.

Every unauthenticated per-IP limit on the public site keys off this. Get it wrong one
way and Heroku's router is the only "visitor" there is, so all customers share one
bucket; get it wrong the other way and anyone can spoof a header to reset their own.
"""

import pytest
from django.test import RequestFactory

from apps.core.net import client_ip


@pytest.fixture
def rf():
    return RequestFactory()


def test_without_a_proxy_the_peer_is_the_client(rf, settings):
    settings.TRUSTED_PROXY_COUNT = 0
    request = rf.get("/", REMOTE_ADDR="203.0.113.7")
    assert client_ip(request) == "203.0.113.7"


def test_without_a_proxy_a_forwarded_header_is_ignored(rf, settings):
    """Nothing in front of us, so X-Forwarded-For is whatever the client felt like."""
    settings.TRUSTED_PROXY_COUNT = 0
    request = rf.get("/", REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR="9.9.9.9")
    assert client_ip(request) == "203.0.113.7"


def test_behind_one_proxy_the_rightmost_entry_wins(rf, settings):
    """Heroku's router appends the connecting peer, so the last entry is the one it
    wrote — and the only one the client could not have written."""
    settings.TRUSTED_PROXY_COUNT = 1
    request = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert client_ip(request) == "203.0.113.7"


def test_a_spoofed_forwarded_header_cannot_hide_the_caller(rf, settings):
    """A client sending its own X-Forwarded-For gets Heroku's entry appended after it.
    Reading from the right ignores everything the client supplied."""
    settings.TRUSTED_PROXY_COUNT = 1
    request = rf.get(
        "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.1.1.1, 2.2.2.2, 203.0.113.7"
    )
    assert client_ip(request) == "203.0.113.7"


def test_two_proxies_step_two_from_the_right(rf, settings):
    settings.TRUSTED_PROXY_COUNT = 2
    request = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.2")
    assert client_ip(request) == "203.0.113.7"


def test_whitespace_and_empty_entries_are_ignored(rf, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    request = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="  ,  203.0.113.7  ,")
    assert client_ip(request) == "203.0.113.7"


def test_a_short_header_falls_back_to_the_peer(rf, settings):
    """Fewer entries than proxies means the header is not what we expect — trusting it
    would let a caller pick their own bucket, so fall back to the peer instead."""
    settings.TRUSTED_PROXY_COUNT = 2
    request = rf.get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert client_ip(request) == "10.0.0.1"


def test_a_missing_header_behind_a_proxy_falls_back_to_the_peer(rf, settings):
    settings.TRUSTED_PROXY_COUNT = 1
    assert client_ip(rf.get("/", REMOTE_ADDR="10.0.0.1")) == "10.0.0.1"


def test_a_request_with_nothing_at_all_is_still_countable(rf, settings):
    settings.TRUSTED_PROXY_COUNT = 0
    request = rf.get("/")
    request.META.pop("REMOTE_ADDR", None)
    assert client_ip(request) == "unknown"
