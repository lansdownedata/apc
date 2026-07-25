import json

from django import forms

# Static service-type options for the booking widget's dropdown (no ServiceType
# model yet). Reservation.service is a free-text CharField, so the value IS the label.
SERVICE_TYPE_CHOICES = [
    ("Airport Transfer", "Airport Transfer"),
    ("Corporate Travel", "Corporate Travel"),
    ("Wedding Transportation", "Wedding Transportation"),
    ("Hourly Charter", "Hourly Charter"),
    ("Group / Shuttle Service", "Group / Shuttle Service"),
    ("Other", "Other"),
]

MAX_STOPS = 10
ADDRESS_MAXLEN = 255
SUITE_MAXLEN = 160


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class BookingRequestForm(forms.Form):
    """Public booking request — a lead comes in through this, no auth required.

    The `company` field is a honeypot: real visitors never see or fill it; bots that
    autofill every field trip it. Address fields feed ordered Stop records: pickup and
    drop-off post as their own fields; optional in-between stops arrive as `stops_json`.
    """

    name = forms.CharField(max_length=200)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=32, required=False)
    pickup_date = forms.DateField(required=False)
    pickup_time = forms.TimeField(required=False)
    passengers = forms.IntegerField(min_value=1, max_value=100, initial=1)
    service = forms.CharField(max_length=120, required=False)
    notes = forms.CharField(widget=forms.Textarea, required=False)
    company = forms.CharField(required=False)  # honeypot — bots fill it

    # Trip ends (posted as plain fields by the address_autocomplete component).
    pickup = forms.CharField(max_length=ADDRESS_MAXLEN, required=False)
    pickup_suite = forms.CharField(max_length=SUITE_MAXLEN, required=False)
    pickup_lat = forms.FloatField(required=False)
    pickup_lng = forms.FloatField(required=False)
    pickup_display = forms.CharField(max_length=512, required=False)
    dropoff = forms.CharField(max_length=ADDRESS_MAXLEN, required=False)
    dropoff_suite = forms.CharField(max_length=SUITE_MAXLEN, required=False)
    dropoff_lat = forms.FloatField(required=False)
    dropoff_lng = forms.FloatField(required=False)
    dropoff_display = forms.CharField(max_length=512, required=False)

    # Optional in-between stops (JSON array from the Alpine repeater).
    stops_json = forms.CharField(required=False)

    def clean_stops_json(self):
        raw = (self.cleaned_data.get("stops_json") or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise forms.ValidationError("Could not read the stops list.") from e
        if not isinstance(data, list):
            raise forms.ValidationError("Stops must be a list.")
        if len(data) > MAX_STOPS:
            raise forms.ValidationError(f"Too many stops (max {MAX_STOPS}).")
        cleaned = []
        for item in data:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or "").strip()[:ADDRESS_MAXLEN]
            if not address:
                continue
            cleaned.append(
                {
                    "address": address,
                    "suite": str(item.get("suite") or "").strip()[:SUITE_MAXLEN],
                    "lat": _to_float(item.get("lat")),
                    "lng": _to_float(item.get("lng")),
                }
            )
        return cleaned

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("company"):
            raise forms.ValidationError("spam detected")
        if not cleaned.get("email") and not cleaned.get("phone"):
            raise forms.ValidationError("Provide an email or phone so we can reach you.")

        # Assemble the ordered trip: pickup -> in-between stops -> drop-off, blanks removed.
        stops = []
        pickup = (cleaned.get("pickup") or "").strip()
        if pickup:
            stops.append(
                {
                    "address": pickup[:ADDRESS_MAXLEN],
                    "suite": (cleaned.get("pickup_suite") or "").strip()[:SUITE_MAXLEN],
                    "lat": cleaned.get("pickup_lat"),
                    "lng": cleaned.get("pickup_lng"),
                }
            )
        stops.extend(cleaned.get("stops_json") or [])
        dropoff = (cleaned.get("dropoff") or "").strip()
        if dropoff:
            stops.append(
                {
                    "address": dropoff[:ADDRESS_MAXLEN],
                    "suite": (cleaned.get("dropoff_suite") or "").strip()[:SUITE_MAXLEN],
                    "lat": cleaned.get("dropoff_lat"),
                    "lng": cleaned.get("dropoff_lng"),
                }
            )
        cleaned["stops"] = stops
        return cleaned
