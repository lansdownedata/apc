import pytest

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
