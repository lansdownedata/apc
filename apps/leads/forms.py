import json

from django import forms

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.leads.models import VehicleType
from apps.public.forms import WeddingRequestForm


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


class PortalWeddingForm(WeddingRequestForm):
    """The public wedding form, minus what the lead already owns and plus vehicles.

    A subclass rather than a copy on purpose: the leg validation — the 12-leg ceiling, the
    1-400 passenger bounds, the server-side vehicle re-derivation — is exactly what must
    never drift between the website and the office, and exactly what is easiest to fork by
    accident. Setting an inherited field to None is Django's documented way to drop it.
    """

    # The lead already has a Contact, and there is no honeypot behind auth.
    name = None
    email = None
    phone = None
    company = None

    # {leg_id: VehicleType pk} — the agent's per-leg choice, seeded from suggest_vehicle().
    vehicles_json = forms.CharField(required=False)

    def clean_vehicles_json(self) -> dict:
        """Unknown, retired and unreadable entries are dropped, never fatal: a vehicle
        retired while the modal was open must not cost the agent the whole day's edits."""
        raw = (self.cleaned_data.get("vehicles_json") or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        wanted = {str(leg): int(pk) for leg, pk in data.items() if str(pk).isdigit()}
        found = {v.pk: v for v in VehicleType.objects.filter(pk__in=wanted.values(), active=True)}
        return {leg: found[pk] for leg, pk in wanted.items() if pk in found}

    def clean(self):
        """No honeypot and no email-or-phone rule; everything else still applies."""
        cleaned = super(WeddingRequestForm, self).clean()
        cleaned["vehicles"] = cleaned.get("vehicles_json") or {}
        return self.resolve_wedding(cleaned)
