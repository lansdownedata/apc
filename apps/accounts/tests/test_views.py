import pytest
from django.urls import reverse

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
