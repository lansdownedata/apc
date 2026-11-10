import pytest
from django.urls import reverse

from apps.accounts.admin import UserAdminForm
from apps.accounts.factories import UserFactory

pytestmark = pytest.mark.django_db


def _form(user, phone: str) -> UserAdminForm:
    data = {
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "phone": phone,
        "date_joined": "2026-01-01 00:00:00",
        "is_active": "on",
    }
    return UserAdminForm(data, instance=user)


def test_admin_normalises_the_phone_to_e164():
    form = _form(UserFactory(phone=""), "(571) 555-0142")
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == "+15715550142"


def test_admin_rejects_a_bad_phone():
    form = _form(UserFactory(phone=""), "12345")
    assert not form.is_valid()
    assert form.errors["phone"] == ["Enter a valid mobile number."]


def test_admin_allows_a_blank_phone():
    form = _form(UserFactory(phone=""), "")
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == ""


def test_the_admin_change_page_uses_the_form(admin_client):
    r = admin_client.get(reverse("admin:accounts_user_change", args=[UserFactory().pk]))
    assert r.status_code == 200
    assert isinstance(r.context["adminform"].form, UserAdminForm)
