"""APC-49 — departments route work; a person may hold several. No permission effect (D1)."""

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department, User, UserDepartment

pytestmark = pytest.mark.django_db


def test_a_user_can_hold_several_departments():
    user = UserFactory()
    UserDepartment.objects.create(user=user, department=Department.SALES)
    UserDepartment.objects.create(user=user, department=Department.ACCOUNTING)

    assert user.department_list == [Department.SALES, Department.ACCOUNTING]


def test_a_duplicate_membership_is_refused():
    user = UserFactory()
    UserDepartment.objects.create(user=user, department=Department.SALES)

    with pytest.raises(IntegrityError), transaction.atomic():
        UserDepartment.objects.create(user=user, department=Department.SALES)


def test_in_department_returns_only_members():
    ops = UserFactory()
    both = UserFactory()
    UserFactory()  # no departments
    UserDepartment.objects.create(user=ops, department=Department.OPERATIONS)
    UserDepartment.objects.create(user=both, department=Department.OPERATIONS)
    UserDepartment.objects.create(user=both, department=Department.SALES)

    assert set(User.objects.in_department(Department.OPERATIONS)) == {ops, both}
    assert list(User.objects.in_department(Department.SALES)) == [both]


def test_users_screen_round_trips_department_membership(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    target = UserFactory()
    UserDepartment.objects.create(user=target, department=Department.SALES)
    url = reverse("user_detail", args=[target.pk])

    resp = client.post(
        url,
        {
            "departments_form": "1",
            "departments": [Department.OPERATIONS, Department.CUSTOMER_SERVICE],
        },
    )

    assert resp.status_code == 302
    assert set(target.department_list) == {Department.OPERATIONS, Department.CUSTOMER_SERVICE}
    body = client.get(url).content.decode()
    assert 'name="departments"' in body
    assert "multiple" in body
    assert f'value="{Department.OPERATIONS}" selected' in body
    assert f'value="{Department.SALES}" selected' not in body


def test_clearing_every_department_is_allowed(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    target = UserFactory()
    UserDepartment.objects.create(user=target, department=Department.SALES)

    client.post(reverse("user_detail", args=[target.pk]), {"departments_form": "1"})

    assert target.department_list == []


def test_unknown_departments_are_ignored(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    target = UserFactory()

    client.post(
        reverse("user_detail", args=[target.pk]),
        {"departments_form": "1", "departments": ["bogus", Department.SALES]},
    )

    assert target.department_list == [Department.SALES]


def test_an_agent_cannot_set_departments(client):
    client.force_login(UserFactory(role=User.Role.AGENT))
    target = UserFactory()

    resp = client.post(
        reverse("user_detail", args=[target.pk]),
        {"departments_form": "1", "departments": [Department.SALES]},
    )

    assert resp.status_code == 403
    assert target.department_list == []
