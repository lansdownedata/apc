from django import forms

from apps.accounts.models import User
from apps.core.choices import Channel


class NewLeadForm(forms.Form):
    """Capture a new lead + its contact from the Leads list modal."""

    name = forms.CharField(max_length=200)
    company = forms.CharField(max_length=200, required=False)
    phone = forms.CharField(max_length=32, required=False)
    email = forms.EmailField(required=False)
    channel = forms.ChoiceField(choices=Channel.choices, initial=Channel.WEBSITE)
    agent = forms.ModelChoiceField(queryset=User.objects.all(), required=False)
