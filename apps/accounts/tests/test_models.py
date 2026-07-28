import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def test_owner_admin_always_has_payments_access():
    owner = UserFactory(role=User.Role.OWNER_ADMIN, can_manage_payments=False)
    assert owner.has_payments_access is True


def test_agent_access_follows_flag():
    agent = UserFactory(role=User.Role.AGENT, can_manage_payments=False)
    assert agent.has_payments_access is False
    agent.can_manage_payments = True
    assert agent.has_payments_access is True


def test_status_is_pending_when_invited_but_not_accepted():
    user = UserFactory(invited_at=timezone.now(), invite_accepted_at=None)
    assert user.status == User.Status.PENDING


def test_status_is_active_once_invite_accepted():
    user = UserFactory(invited_at=timezone.now(), invite_accepted_at=timezone.now())
    assert user.status == User.Status.ACTIVE


def test_status_is_active_for_preexisting_user_with_no_invite():
    """Accounts created before invites existed must not read as Pending."""
    user = UserFactory(invited_at=None, invite_accepted_at=None)
    assert user.status == User.Status.ACTIVE


def test_status_is_deactivated_regardless_of_invite_state():
    """is_active outranks the invite fields."""
    user = UserFactory(is_active=False, invited_at=timezone.now(), invite_accepted_at=None)
    assert user.status == User.Status.DEACTIVATED


def test_invited_by_survives_inviter_deletion():
    inviter = UserFactory(role=User.Role.OWNER_ADMIN)
    invitee = UserFactory(invited_by=inviter)
    inviter.delete()
    invitee.refresh_from_db()
    assert invitee.invited_by is None
