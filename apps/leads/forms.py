from django import forms

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164


class NewLeadForm(forms.Form):
    """Capture a new lead + its contact from the Leads list modal."""

    name = forms.CharField(max_length=200)
    # Set when the agent picked someone from the modal's customer search. That contact is
    # then used as-is — no phone/email dedupe guessing — and the fields above are written
    # back to their profile. Blank means "create/match a contact from what was typed".
    contact_id = forms.ModelChoiceField(
        queryset=Contact.objects.all(),
        required=False,
        error_messages={"invalid_choice": "That customer no longer exists."},
    )
    company = forms.CharField(max_length=200, required=False)
    phone = forms.CharField(max_length=32, required=False)
    email = forms.EmailField(required=False)
    channel = forms.ChoiceField(choices=Channel.choices, initial=Channel.WEBSITE)
    agent = forms.ModelChoiceField(queryset=User.objects.all(), required=False)
    # "booking" = the New booking button: skip the website-worded welcome touch-points and
    # land on the workspace flagged as a booking in progress (spec 2026-08-29 §5).
    intent = forms.ChoiceField(choices=[("lead", "lead"), ("booking", "booking")], required=False)

    def clean_phone(self) -> str:
        """Store E.164 so the contact matches Podium's inbound identifier."""
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        normalized = to_e164(raw)
        if normalized is None:
            raise forms.ValidationError("Enter a valid phone number.")
        return normalized
