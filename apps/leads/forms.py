from django import forms

from apps.accounts.models import User
from apps.core.choices import Channel
from apps.core.phone import to_e164


class NewLeadForm(forms.Form):
    """Capture a new lead + its contact from the Leads list modal."""

    name = forms.CharField(max_length=200)
    company = forms.CharField(max_length=200, required=False)
    phone = forms.CharField(max_length=32, required=False)
    email = forms.EmailField(required=False)
    channel = forms.ChoiceField(choices=Channel.choices, initial=Channel.WEBSITE)
    agent = forms.ModelChoiceField(queryset=User.objects.all(), required=False)

    def clean_phone(self) -> str:
        """Normalize to E.164 up front so `match_or_create` gets a clean value."""
        raw = self.cleaned_data.get("phone", "").strip()
        if not raw:
            return ""
        e164 = to_e164(raw)
        if e164 is None:
            raise forms.ValidationError("Enter a valid phone number.")
        return e164
