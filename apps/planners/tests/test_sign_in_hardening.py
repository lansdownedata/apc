"""Review fixes: sign-in timing and per-account lockout (APC-66)."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import get_hasher
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.planners import services
from apps.planners.auth import SESSION_KEY
from apps.planners.models import PlannerAccount

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-pass-phrase-77"


@pytest.fixture
def registered():
    contact = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    return services.register(account, PASSWORD)


@pytest.fixture
def invited():
    contact = ContactFactory(name="Ivy Invited", email="ivy@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    return account


def _hashes(email: str, password: str) -> int:
    hasher = type(get_hasher())
    with patch.object(hasher, "encode", autospec=True, side_effect=hasher.encode) as encode:
        services.authenticate(email, password)
    return encode.call_count


def test_the_hasher_runs_once_whoever_the_email_belongs_to(registered, invited):
    wrong = _hashes("dana@example.com", "wrong-password-1")
    assert wrong == 1
    assert _hashes("nobody@example.com", "wrong-password-1") == wrong
    assert _hashes("ivy@example.com", "wrong-password-1") == wrong
    services.revoke(registered, actor=UserFactory())
    assert _hashes("dana@example.com", PASSWORD) == wrong


def test_ten_failures_lock_the_account_for_fifteen_minutes(registered):
    for _ in range(services.LOCKOUT_FAILURES):
        assert services.authenticate("dana@example.com", "wrong-password-1") is None
    registered.refresh_from_db()
    assert registered.locked_until is not None
    # the right password is refused while locked, the same way a wrong one is
    assert services.authenticate("dana@example.com", PASSWORD) is None
    PlannerAccount.objects.filter(pk=registered.pk).update(
        locked_until=timezone.now() - timedelta(seconds=1)
    )
    assert services.authenticate("dana@example.com", PASSWORD) == registered


def test_a_successful_sign_in_resets_the_count(registered):
    for _ in range(services.LOCKOUT_FAILURES - 1):
        services.authenticate("dana@example.com", "wrong-password-1")
    assert services.authenticate("dana@example.com", PASSWORD) == registered
    registered.refresh_from_db()
    assert registered.failed_logins == 0
    services.authenticate("dana@example.com", "wrong-password-1")
    assert services.authenticate("dana@example.com", PASSWORD) == registered


def test_a_locked_account_looks_exactly_like_an_unknown_email(client, registered, settings):
    for _ in range(services.LOCKOUT_FAILURES):
        services.authenticate("dana@example.com", "wrong-password-1")
    locked = client.post(
        reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD}
    )
    unknown = client.post(
        reverse("planners:login"), {"email": "nobody@example.com", "password": PASSWORD}
    )
    assert locked.status_code == unknown.status_code == 200
    strip = lambda r, e: r.content.decode().replace(e, "")  # noqa: E731
    assert "don't match" in strip(locked, "dana@example.com")
    assert (
        strip(locked, "dana@example.com").split("<form")[0]
        == strip(unknown, "nobody@example.com").split("<form")[0]
    )
    assert SESSION_KEY not in client.session


def test_setting_a_new_password_clears_a_lock(registered):
    for _ in range(services.LOCKOUT_FAILURES):
        services.authenticate("dana@example.com", "wrong-password-1")
    services.register(registered, "a-brand-new-phrase-31")
    assert services.authenticate("dana@example.com", "a-brand-new-phrase-31") == registered
