import pytest

from apps.accounts.factories import UserFactory
from apps.accounts.forms import AcceptInviteForm, UserInviteForm
from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _data(**overrides):
    base = {
        "first_name": "New",
        "last_name": "Hire",
        "email": "New.Hire@allprocharter.com",
        "role": User.Role.AGENT,
        "can_manage_payments": False,
    }
    base.update(overrides)
    return base


def test_email_is_normalised_to_lowercase():
    form = UserInviteForm(data=_data())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["email"] == "new.hire@allprocharter.com"


def test_duplicate_email_is_a_form_error_not_a_crash():
    UserFactory(username="taken@allprocharter.com", email="taken@allprocharter.com")
    form = UserInviteForm(data=_data(email="taken@allprocharter.com"))
    assert not form.is_valid()
    assert "email" in form.errors


def test_duplicate_email_detection_ignores_case():
    UserFactory(username="taken@allprocharter.com", email="taken@allprocharter.com")
    form = UserInviteForm(data=_data(email="TAKEN@allprocharter.com"))
    assert not form.is_valid()
    assert "email" in form.errors


def test_duplicate_is_caught_when_only_the_username_matches():
    """Older accounts may have the address as username with email left blank."""
    UserFactory(username="taken@allprocharter.com", email="")
    form = UserInviteForm(data=_data(email="taken@allprocharter.com"))
    assert not form.is_valid()
    assert "email" in form.errors


def test_email_is_required():
    form = UserInviteForm(data=_data(email=""))
    assert not form.is_valid()
    assert "email" in form.errors


def test_first_name_is_required():
    form = UserInviteForm(data=_data(first_name=""))
    assert not form.is_valid()
    assert "first_name" in form.errors


def test_unknown_role_is_rejected():
    form = UserInviteForm(data=_data(role="superuser"))
    assert not form.is_valid()
    assert "role" in form.errors


def test_accept_invite_form_rejects_mismatched_passwords():
    user = UserFactory()
    form = AcceptInviteForm(user, data={"new_password1": "sW9!kdo2Lm", "new_password2": "nope"})
    assert not form.is_valid()


def test_accept_invite_form_sets_a_usable_password():
    user = UserFactory()
    user.set_unusable_password()
    user.save()
    form = AcceptInviteForm(
        user, data={"new_password1": "sW9!kdo2Lm", "new_password2": "sW9!kdo2Lm"}
    )
    assert form.is_valid(), form.errors
    form.save()
    user.refresh_from_db()
    assert user.has_usable_password() is True
