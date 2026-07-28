import pytest

from apps.accounts import services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def test_cannot_demote_the_last_active_admin():
    """Actor is deliberately someone else: the self-guard runs first and would mask this."""
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    actor = UserFactory(role=User.Role.AGENT)
    with pytest.raises(services.UserManagementError, match="last admin"):
        services.change_user_role(target=admin, new_role=User.Role.AGENT, actor=actor)


def test_can_demote_an_admin_when_another_active_admin_exists():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    other = UserFactory(role=User.Role.OWNER_ADMIN)
    services.change_user_role(target=other, new_role=User.Role.AGENT, actor=admin)
    other.refresh_from_db()
    assert other.role == User.Role.AGENT


def test_a_deactivated_admin_does_not_count_as_cover():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    UserFactory(role=User.Role.OWNER_ADMIN, deactivated=True)
    actor = UserFactory(role=User.Role.AGENT)
    with pytest.raises(services.UserManagementError, match="last admin"):
        services.change_user_role(target=admin, new_role=User.Role.AGENT, actor=actor)


def test_self_guard_takes_precedence_over_the_last_admin_message():
    """A sole admin demoting themselves gets the self message — the more actionable one."""
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    with pytest.raises(services.UserManagementError, match="your own"):
        services.change_user_role(target=admin, new_role=User.Role.AGENT, actor=admin)


def test_cannot_demote_yourself():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    UserFactory(role=User.Role.OWNER_ADMIN)  # cover, so last-admin is not what trips
    with pytest.raises(services.UserManagementError, match="your own"):
        services.change_user_role(target=admin, new_role=User.Role.AGENT, actor=admin)


def test_cannot_deactivate_yourself():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    UserFactory(role=User.Role.OWNER_ADMIN)
    with pytest.raises(services.UserManagementError, match="your own"):
        services.set_user_active(target=admin, active=False, actor=admin)


def test_cannot_deactivate_the_last_active_admin():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    actor = UserFactory(role=User.Role.OWNER_ADMIN)
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    with pytest.raises(services.UserManagementError, match="last admin"):
        services.set_user_active(target=admin, active=False, actor=actor)


def test_reactivating_a_user_is_allowed():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    target = UserFactory(role=User.Role.AGENT, deactivated=True)
    services.set_user_active(target=target, active=True, actor=admin)
    target.refresh_from_db()
    assert target.is_active is True


def test_rejects_an_unknown_role():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    target = UserFactory(role=User.Role.AGENT)
    with pytest.raises(services.UserManagementError, match="role"):
        services.change_user_role(target=target, new_role="superuser", actor=admin)


def test_promoting_an_agent_to_admin_is_allowed():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    target = UserFactory(role=User.Role.AGENT)
    services.change_user_role(target=target, new_role=User.Role.OWNER_ADMIN, actor=admin)
    target.refresh_from_db()
    assert target.role == User.Role.OWNER_ADMIN


def test_active_admin_count_ignores_agents_and_inactive_admins():
    UserFactory(role=User.Role.OWNER_ADMIN)
    UserFactory(role=User.Role.OWNER_ADMIN, deactivated=True)
    UserFactory(role=User.Role.AGENT)
    assert services.active_admin_count() == 1
