"""User-management operations and the guards that keep the portal reachable.

Guards live here rather than in views so "the last admin cannot be removed" is a
property of the domain, enforced for any caller — HTTP, shell, or a future API.
"""

from __future__ import annotations

from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.notifications.email import send_html_email

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


def build_invite_link(user: User) -> str:
    """Absolute set-password URL for this user.

    The token hashes the user's password and last_login, so it dies the moment a
    password is set — single-use needs no bookkeeping of our own.
    """
    path = reverse(
        "accept_invite",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )
    return urljoin(settings.PUBLIC_BASE_URL, path)


def _send_invite_email(user: User) -> bool:
    return send_html_email(
        to=user.email,
        subject=f"You've been invited to the {settings.COMPANY_NAME} Lead Manager",
        template="user_invite",
        context={
            "first_name": user.first_name,
            "invite_url": build_invite_link(user),
            "role_label": user.get_role_display(),
            "company_name": settings.COMPANY_NAME,
            "company_email": settings.COMPANY_EMAIL,
            "company_phone": settings.COMPANY_PHONE,
        },
    )


def invite_user(
    *,
    first_name: str,
    last_name: str,
    email: str,
    role: str,
    can_manage_payments: bool,
    actor: User,
) -> tuple[User, bool]:
    """Create a pending staff account and email a set-password link.

    Returns (user, email_sent). The user is kept even when delivery fails so the
    admin can hit Resend rather than re-entering everything.
    """
    if role not in User.Role.values:
        raise UserManagementError(f"Unknown role {role!r}.")

    email = (email or "").strip().lower()
    user = User(
        username=email,
        email=email,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        role=role,
        can_manage_payments=can_manage_payments,
        invited_at=timezone.now(),
        invited_by=actor,
    )
    user.set_unusable_password()
    user.save()
    return user, _send_invite_email(user)


def _assert_pending(target: User) -> None:
    if target.status != User.Status.PENDING:
        raise UserManagementError(
            "This user is not pending — they have already accepted their invite."
        )


def resend_invite(*, target: User) -> bool:
    """Issue a fresh link.

    The previous link keeps working until it expires: the token depends on the password
    and last_login, neither of which a resend changes. Both belong to the same invited
    person, so this is acceptable.
    """
    _assert_pending(target)
    target.invited_at = timezone.now()
    target.save(update_fields=["invited_at"])
    return _send_invite_email(target)


def revoke_invite(*, target: User) -> None:
    """Delete a pending user. They never signed in and own no data."""
    _assert_pending(target)
    target.delete()
