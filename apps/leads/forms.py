import json
from decimal import Decimal

from django import forms

from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.leads.models import VehicleType
from apps.public.forms import WeddingRequestForm
from apps.reservations.groups import DUPLICATE_MAX
from apps.reservations.models import Reservation


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
    # "booking" = the New booking button, "wedding" = New wedding. Both skip the
    # website-worded welcome touch-points and land on the workspace ready to build
    # (specs 2026-08-29 §5 and 2026-08-30 §5.1).
    intent = forms.ChoiceField(
        choices=[("lead", "lead"), ("booking", "booking"), ("wedding", "wedding")],
        required=False,
    )

    def clean_phone(self) -> str:
        """Store E.164 so the contact matches Podium's inbound identifier."""
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        normalized = to_e164(raw)
        if normalized is None:
            raise forms.ValidationError("Enter a valid phone number.")
        return normalized


# The agent's per-leg overrides all post as {leg_id: value} JSON. Three of them now
# (vehicle, trip type, hours), so the parsing lives in one place.
MIN_BILLED_HOURS = Decimal("1")
MAX_BILLED_HOURS = Decimal("24")


def _leg_map(raw: str | None, coerce) -> dict:
    """`{leg_id: value}` from a posted JSON object, dropping whatever `coerce` rejects.

    Never fatal by design: these are per-leg overrides on a form the agent may have had
    open for a while, and one stale or unreadable entry must not cost them the whole
    day's edits. The leg simply keeps what it already had.
    """
    try:
        data = json.loads((raw or "").strip() or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for leg, value in data.items():
        coerced = coerce(value)
        if coerced is not None:
            cleaned[str(leg)] = coerced
    return cleaned


def _as_pk(value):
    return int(value) if str(value).isdigit() else None


def _as_trip_type(value):
    return str(value) if str(value) in Reservation.TripType.values else None


def _as_vehicle_count(value):
    """How many vehicles the agent wants on a leg (APC-14). Out of range or unreadable →
    None, which leaves the leg on the count the coach maths derives."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if 1 <= count <= DUPLICATE_MAX else None


def _as_billed_hours(value):
    """The same 1-24 window the public booking widget enforces."""
    try:
        hours = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return hours if MIN_BILLED_HOURS <= hours <= MAX_BILLED_HOURS else None


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

    # All three keyed by leg id. Absent means "leave this leg as it is", which is what
    # keeps a rebuild after a time change from silently un-pricing the day.
    vehicles_json = forms.CharField(required=False)
    trip_types_json = forms.CharField(required=False)
    hours_json = forms.CharField(required=False)
    # Also keyed by leg id, and also absent-means-leave-alone — except that "as it is"
    # for a count is the number the coach maths derives from the headcount, not the
    # number stored, so a leg whose guest list grew still grows its set (APC-14).
    counts_json = forms.CharField(required=False)

    def clean_vehicles_json(self) -> dict:
        """Unknown and retired vehicles are dropped, never fatal — see `_leg_map`."""
        wanted = _leg_map(self.cleaned_data.get("vehicles_json"), _as_pk)
        found = {
            v.pk: v for v in VehicleType.objects.filter(pk__in=set(wanted.values()), active=True)
        }
        return {leg: found[pk] for leg, pk in wanted.items() if pk in found}

    def clean_trip_types_json(self) -> dict:
        return _leg_map(self.cleaned_data.get("trip_types_json"), _as_trip_type)

    def clean_hours_json(self) -> dict:
        return _leg_map(self.cleaned_data.get("hours_json"), _as_billed_hours)

    def clean_counts_json(self) -> dict:
        return _leg_map(self.cleaned_data.get("counts_json"), _as_vehicle_count)

    def clean(self):
        """No honeypot and no email-or-phone rule; everything else still applies."""
        cleaned = super(WeddingRequestForm, self).clean()
        cleaned["vehicles"] = cleaned.get("vehicles_json") or {}
        cleaned["trip_types"] = cleaned.get("trip_types_json") or {}
        cleaned["hours"] = cleaned.get("hours_json") or {}
        cleaned["counts"] = cleaned.get("counts_json") or {}
        return self.resolve_wedding(cleaned)
