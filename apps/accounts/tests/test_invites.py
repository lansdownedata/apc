from unittest.mock import patch

import pytest
from django.contrib.auth.tokens import default_token_generator

from apps.accounts import services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return UserFactory(role=User.Role.OWNER_ADMIN)


def _invite(admin, email="new.hire@allprocharter.com", role=User.Role.AGENT):
    return services.invite_user(
        first_name="New",
        last_name="Hire",
        email=email,
        role=role,
        can_manage_payments=False,
        actor=admin,
    )


def test_invite_creates_user_with_email_as_username_and_no_usable_password(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, sent = _invite(admin)
    assert sent is True
    assert user.username == "new.hire@allprocharter.com"
    assert user.email == "new.hire@allprocharter.com"
    assert user.has_usable_password() is False
    assert user.invited_at is not None
    assert user.invite_accepted_at is None
    assert user.invited_by == admin
    assert user.status == User.Status.PENDING


def test_invite_normalises_the_email_to_lowercase(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, _ = _invite(admin, email="New.Hire@AllProCharter.com")
    assert user.email == "new.hire@allprocharter.com"
    assert user.username == "new.hire@allprocharter.com"


def test_invite_sends_one_email_to_the_invitee(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True) as send:
        _invite(admin)
    send.assert_called_once()
    assert send.call_args.kwargs["to"] == "new.hire@allprocharter.com"
    assert send.call_args.kwargs["template"] == "user_invite"
    assert "invite_url" in send.call_args.kwargs["context"]


def test_invite_reports_a_failed_send_but_keeps_the_user(admin):
    """A swallowed failure would tell the admin someone was emailed when they weren't."""
    with patch("apps.accounts.services.send_html_email", return_value=False):
        user, sent = _invite(admin)
    assert sent is False
    assert User.objects.filter(pk=user.pk).exists()


def test_invite_link_carries_a_valid_token(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, _ = _invite(admin)
    link = services.build_invite_link(user)
    token = link.rstrip("/").rsplit("/", 1)[-1]
    assert default_token_generator.check_token(user, token) is True


def test_invite_rejects_an_unknown_role(admin):
    with pytest.raises(services.UserManagementError, match="role"):
        _invite(admin, role="superuser")


def test_resend_restamps_invited_at_and_sends_again(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, _ = _invite(admin)
    first = user.invited_at
    with patch("apps.accounts.services.send_html_email", return_value=True) as send:
        assert services.resend_invite(target=user) is True
    user.refresh_from_db()
    assert user.invited_at > first
    send.assert_called_once()


def test_resend_is_refused_for_an_accepted_user(admin):
    user = UserFactory(invited_at=None, invite_accepted_at=None)
    with pytest.raises(services.UserManagementError, match="pending"):
        services.resend_invite(target=user)


def test_revoke_deletes_a_pending_user(admin):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, _ = _invite(admin)
    pk = user.pk
    services.revoke_invite(target=user)
    assert not User.objects.filter(pk=pk).exists()


def test_revoke_is_refused_for_an_accepted_user(admin):
    user = UserFactory(role=User.Role.AGENT)
    with pytest.raises(services.UserManagementError, match="pending"):
        services.revoke_invite(target=user)
