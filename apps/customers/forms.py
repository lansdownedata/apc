"""Forms for the customer's own sign-up and password pages."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError

from apps.accounts.forms import FIELD_CLASS, INVALID_PHONE
from apps.core.phone import to_e164

# The set-password screen promises "At least 10 characters."; the site-wide minimum is 8.
MIN_PASSWORD_LENGTH = 10


class SignupForm(forms.Form):
    """Step 1 of sign-up. The mobile is optional; when given it must be a real number."""

    name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)
    phone = forms.CharField(max_length=40, required=False)

    def clean_phone(self) -> str:
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and to_e164(phone) is None:
            raise ValidationError(INVALID_PHONE)
        return phone


def _validators() -> list:
    """The site's password validators, with the customer screen's longer minimum."""
    rest = [
        v for v in get_default_password_validators() if not isinstance(v, MinimumLengthValidator)
    ]
    return [MinimumLengthValidator(MIN_PASSWORD_LENGTH), *rest]


class CustomerPasswordForm(SetPasswordForm):
    """Django's set-password form for a customer (an invite, a reset or a sign-up link).

    `user` is the account, or None on a sign-up link, where no account exists yet."""

    def __init__(self, user: object | None, *args: object, **kwargs: object) -> None:
        super().__init__(user, *args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = FIELD_CLASS

    def validate_password_for_user(
        self, user: object | None, password_field_name: str = "new_password2"
    ) -> None:
        """Django's check, with the customer rules, and the error shown under Password
        (beside the "At least 10 characters." hint) rather than under Confirm password."""
        password = self.cleaned_data.get(password_field_name)
        if password:
            try:
                validate_password(password, user, password_validators=_validators())
            except ValidationError as error:
                self.add_error("new_password1", error)
