"""User-management operations and the guards that keep the portal reachable.

Guards live here rather than in views so "the last admin cannot be removed" is a
property of the domain, enforced for any caller — HTTP, shell, or a future API.
"""

from __future__ import annotations

from .models import User


class UserManagementError(Exception):
    """A refused user-management operation, safe to show to the operator."""


def active_admin_count(exclude_pk: int | None = None) -> int:
    """Admins who can currently sign in. A deactivated admin is not cover."""
    qs = User.objects.filter(role=User.Role.OWNER_ADMIN, is_active=True)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()


def _assert_not_last_admin(target: User) -> None:
    if (
        target.role == User.Role.OWNER_ADMIN
        and target.is_active
        and active_admin_count(exclude_pk=target.pk) == 0
    ):
        raise UserManagementError(
            "This is the last admin — promote another user first, "
            "or you will lock everyone out of the portal."
        )


def _assert_not_self(target: User, actor: User) -> None:
    if target.pk == actor.pk:
        raise UserManagementError("You cannot change your own role or access.")


def change_user_role(*, target: User, new_role: str, actor: User) -> User:
    """Move a user between roles, refusing anything that would cause a lockout."""
    if new_role not in User.Role.values:
        raise UserManagementError(f"Unknown role {new_role!r}.")
    _assert_not_self(target, actor)
    if new_role != User.Role.OWNER_ADMIN:
        _assert_not_last_admin(target)
    target.role = new_role
    target.save(update_fields=["role"])
    return target


def set_user_active(*, target: User, active: bool, actor: User) -> User:
    """Deactivate or reactivate a user. Reactivation is always safe."""
    if not active:
        _assert_not_self(target, actor)
        _assert_not_last_admin(target)
    target.is_active = active
    target.save(update_fields=["is_active"])
    return target
