from unittest.mock import patch

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def test_user_list_owner_admin_only(client):
    agent = UserFactory(role=User.Role.AGENT)
    client.force_login(agent)
    assert client.get(reverse("user_list")).status_code == 403
    owner = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(owner)
    assert client.get(reverse("user_list")).status_code == 200


def test_toggle_grants_payment_access(client):
    owner = UserFactory(role=User.Role.OWNER_ADMIN)
    target = UserFactory(role=User.Role.AGENT, can_manage_payments=False)
    client.force_login(owner)
    resp = client.post(
        reverse("user_detail", args=[target.pk]),
        {"capability": "can_manage_payments", "enabled": "on"},
    )
    assert resp.status_code in (200, 302)
    target.refresh_from_db()
    assert target.can_manage_payments is True


# --- invite / accept / resend / revoke / activation ------------------------------------


def _invite_payload(**overrides):
    data = {
        "first_name": "New",
        "last_name": "Hire",
        "email": "new.hire@allprocharter.com",
        "role": User.Role.AGENT,
    }
    data.update(overrides)
    return data


def _make_pending(admin, email="new.hire@allprocharter.com"):
    with patch("apps.accounts.services.send_html_email", return_value=True):
        user, _ = services.invite_user(
            first_name="New",
            last_name="Hire",
            email=email,
            role=User.Role.AGENT,
            can_manage_payments=False,
            actor=admin,
        )
    return user


def _accept_url(user):
    return reverse(
        "accept_invite",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": default_token_generator.make_token(user),
        },
    )


def test_invite_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))
    assert client.post(reverse("user_invite"), _invite_payload()).status_code == 403


def test_invite_creates_a_pending_user(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch("apps.accounts.services.send_html_email", return_value=True):
        resp = client.post(reverse("user_invite"), _invite_payload())
    assert resp.status_code in (200, 302)
    assert User.objects.get(email="new.hire@allprocharter.com").status == User.Status.PENDING


def test_invite_reports_a_failed_email_without_losing_the_user(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch("apps.accounts.services.send_html_email", return_value=False):
        client.post(reverse("user_invite"), _invite_payload(), follow=True)
    assert User.objects.filter(email="new.hire@allprocharter.com").exists()


def test_invite_with_duplicate_email_shows_an_error(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    UserFactory(username="dupe@allprocharter.com", email="dupe@allprocharter.com")
    before = User.objects.count()
    client.post(
        reverse("user_invite"), _invite_payload(email="dupe@allprocharter.com"), follow=True
    )
    assert User.objects.count() == before


def test_accept_invite_sets_password_and_marks_accepted(client):
    user = _make_pending(UserFactory(role=User.Role.OWNER_ADMIN))
    url = _accept_url(user)
    assert client.get(url).status_code == 200
    resp = client.post(url, {"new_password1": "sW9!kdo2Lm", "new_password2": "sW9!kdo2Lm"})
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.has_usable_password() is True
    assert user.invite_accepted_at is not None
    assert user.status == User.Status.ACTIVE


def test_invite_token_cannot_be_reused(client):
    """Setting the password changes the hash the token is derived from."""
    user = _make_pending(UserFactory(role=User.Role.OWNER_ADMIN))
    url = _accept_url(user)
    client.post(url, {"new_password1": "sW9!kdo2Lm", "new_password2": "sW9!kdo2Lm"})
    assert client.get(url).status_code == 400


def test_accept_invite_with_a_tampered_token_is_rejected_not_a_500(client):
    user = UserFactory()
    url = reverse(
        "accept_invite",
        kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)), "token": "bogus-token"},
    )
    assert client.get(url).status_code == 400


def test_accept_invite_with_a_garbage_uid_is_rejected(client):
    url = reverse("accept_invite", kwargs={"uidb64": "zzzz", "token": "bogus-token"})
    assert client.get(url).status_code == 400


def test_resend_invite_sends_again(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    user = _make_pending(admin)
    with patch("apps.accounts.services.send_html_email", return_value=True) as send:
        client.post(reverse("user_resend_invite", args=[user.pk]), follow=True)
    send.assert_called_once()


def test_revoke_invite_deletes_the_pending_user(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    user = _make_pending(admin)
    client.post(reverse("user_revoke_invite", args=[user.pk]), follow=True)
    assert not User.objects.filter(pk=user.pk).exists()


def test_an_admin_can_deactivate_another_admin_when_cover_remains(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    other = UserFactory(role=User.Role.OWNER_ADMIN)
    client.post(reverse("user_set_active", args=[other.pk]), {"active": "0"}, follow=True)
    other.refresh_from_db()
    assert other.is_active is False


def test_the_view_refuses_self_deactivation(client):
    """The only way lockout is reachable over HTTP: owner_admin_required means the actor
    is always an active admin, so a distinct target always has that actor as cover."""
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    client.post(reverse("user_set_active", args=[admin.pk]), {"active": "0"}, follow=True)
    admin.refresh_from_db()
    assert admin.is_active is True


def test_role_change_goes_through_the_guard(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    target = UserFactory(role=User.Role.AGENT)
    client.post(reverse("user_detail", args=[target.pk]), {"role": User.Role.OWNER_ADMIN})
    target.refresh_from_db()
    assert target.role == User.Role.OWNER_ADMIN


# --- list rendering --------------------------------------------------------------------


def test_user_list_orders_pending_first(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN, username="zz-admin"))
    UserFactory(username="aa-active", role=User.Role.AGENT)
    pending = UserFactory(username="zz-pending", role=User.Role.AGENT, pending=True)
    resp = client.get(reverse("user_list"))
    assert list(resp.context["users"])[0] == pending


def test_user_list_exposes_the_invite_form(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.get(reverse("user_list"))
    assert "invite_form" in resp.context
    assert "roles" in resp.context


def test_user_list_shows_status_badges(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    UserFactory(username="p@x.com", pending=True)
    UserFactory(username="d@x.com", deactivated=True)
    html = client.get(reverse("user_list")).content.decode()
    assert "Pending" in html
    assert "Deactivated" in html


def test_user_detail_shows_resend_and_revoke_for_pending(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(admin)
    user = _make_pending(admin)
    html = client.get(reverse("user_detail", args=[user.pk])).content.decode()
    assert "Resend invite" in html
    assert "Revoke invite" in html


def test_user_detail_shows_deactivate_for_active_user(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    target = UserFactory(role=User.Role.AGENT)
    html = client.get(reverse("user_detail", args=[target.pk])).content.decode()
    assert "Deactivate" in html
    assert "Resend invite" not in html
