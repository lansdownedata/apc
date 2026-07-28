"""Forms for staff invitation and invite acceptance."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.db.models import Q

from .models import User

FIELD_CLASS = "field"


class UserInviteForm(forms.ModelForm):
    """Collects the new staff member's details. The email doubles as the username."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "role", "can_manage_payments"]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": FIELD_CLASS, "autocomplete": "off"}),
            "last_name": forms.TextInput(attrs={"class": FIELD_CLASS, "autocomplete": "off"}),
            "email": forms.EmailInput(attrs={"class": FIELD_CLASS, "autocomplete": "off"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
        self.fields["first_name"].required = True

    def clean_email(self) -> str:
        """Normalise and reject duplicates.

        Checked against username as well as email: the invite sets both to the same
        address, and an older account may have only one of them populated.
        """
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("An email address is required to send the invite.")
        clash = User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email))
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("A user with this email address already exists.")
        return email


class AcceptInviteForm(SetPasswordForm):
    """Django's password-setting form, subclassed so validators and messages stay standard."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = FIELD_CLASS
