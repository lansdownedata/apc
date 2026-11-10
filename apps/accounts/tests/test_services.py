import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from apps.accounts import services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.audit.context import audit_context
from apps.audit.models import AuditEntry

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


def _user_entries(user: User):
    ct = ContentType.objects.get_for_model(User)
    return AuditEntry.objects.filter(content_type=ct, object_id=user.pk)


def test_invite_user_stores_the_phone():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    user, _ = services.invite_user(
        first_name="New",
        last_name="Hire",
        email="new.hire@allprocharter.com",
        role=User.Role.AGENT,
        phone="+15715550142",
        actor=admin,
    )
    assert User.objects.get(pk=user.pk).phone == "+15715550142"


def test_invite_user_rejects_an_invalid_phone():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    with pytest.raises(services.UserManagementError, match="Enter a valid mobile number."):
        services.invite_user(
            first_name="New",
            last_name="Hire",
            email="new.hire@allprocharter.com",
            role=User.Role.AGENT,
            phone="12345",
            actor=admin,
        )
    assert not User.objects.filter(email="new.hire@allprocharter.com").exists()


def test_change_user_phone_writes_an_audit_entry():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    user = UserFactory(phone="+15715550142")
    with audit_context(actor=admin, source="test"):
        services.change_user_phone(target=user, phone="571-555-0199", actor=admin)
    entry = _user_entries(user).get(action=AuditEntry.Action.UPDATE)
    assert entry.changes["phone"] == ["+15715550142", "+15715550199"]
    assert entry.actor == admin


def test_change_user_phone_rejects_invalid():
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    user = UserFactory(phone="+15715550142")
    with pytest.raises(services.UserManagementError, match="Enter a valid mobile number."):
        services.change_user_phone(target=user, phone="12345", actor=admin)
    user.refresh_from_db()
    assert user.phone == "+15715550142"


def test_login_bookkeeping_writes_no_audit_entry():
    user = UserFactory()
    before = _user_entries(user).filter(action=AuditEntry.Action.UPDATE).count()
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    assert _user_entries(user).filter(action=AuditEntry.Action.UPDATE).count() == before


def test_user_has_no_two_factor_enabled_field():
    assert "two_factor_enabled" not in {f.name for f in User._meta.get_fields()}
