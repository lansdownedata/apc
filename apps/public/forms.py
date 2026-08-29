import json
import re

from django import forms

from apps.addresses.models import Airline, Airport
from apps.reservations.models import Reservation

# Optional "occasion" for the booking widget's dropdown (no ServiceType model yet).
# Reservation.service is a free-text CharField, so the value IS the label. Trip *type*
# (transfer/hourly) is a separate field posting Reservation.trip_type — these are the
# occasions that used to be conflated with it in a single six-option select.
OCCASION_CHOICES = [
    ("Airport Transfer", "Airport Transfer"),
    ("Corporate Travel", "Corporate Travel"),
    ("Wedding Transportation", "Wedding Transportation"),
    ("Group / Shuttle Service", "Group / Shuttle Service"),
    ("Other", "Other"),
]

MAX_STOPS = 4
ADDRESS_MAXLEN = 255
FLIGHT_RE = re.compile(r"^\d{1,6}$")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _flight_fields(airport_id, airline_id, flight) -> dict:
    """Airline / flight only mean anything at an airport (same rule as drafts.parse_draft)."""
    if not airport_id:
        return {"airport_id": None, "airline_id": None, "flight_number": ""}
    return {"airport_id": airport_id, "airline_id": airline_id, "flight_number": flight or ""}


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
    # trip_type is required=False with a transfer fallback rather than a required
    # ChoiceField: it matches Reservation.trip_type's model default, and it keeps a
    # POST that predates the toggle (or omits it) valid instead of erroring.
    trip_type = forms.ChoiceField(choices=Reservation.TripType.choices, required=False)
    hours = forms.DecimalField(min_value=1, max_value=24, required=False)
    service = forms.CharField(max_length=120, required=False)
    notes = forms.CharField(widget=forms.Textarea, required=False)
    company = forms.CharField(required=False)  # honeypot — bots fill it

    # Trip ends (posted as plain fields by the address_autocomplete component).
    pickup = forms.CharField(max_length=ADDRESS_MAXLEN, required=False)
    pickup_lat = forms.FloatField(required=False)
    pickup_lng = forms.FloatField(required=False)
    pickup_display = forms.CharField(max_length=512, required=False)
    dropoff = forms.CharField(max_length=ADDRESS_MAXLEN, required=False)
    dropoff_lat = forms.FloatField(required=False)
    dropoff_lng = forms.FloatField(required=False)
    dropoff_display = forms.CharField(max_length=512, required=False)

    # Flight info (spec 2026-08-28) — set by the widget only when the address was picked
    # from the airport directory. Validated like reservations.drafts: real airport,
    # active airline, digits-only flight number; all dropped when there is no airport.
    pickup_airport = forms.IntegerField(required=False)
    pickup_airline = forms.ModelChoiceField(
        queryset=Airline.objects.filter(is_active=True), required=False
    )
    pickup_flight = forms.RegexField(
        regex=FLIGHT_RE,
        required=False,
        error_messages={"invalid": "Flight number must be digits (up to 6)."},
    )
    dropoff_airport = forms.IntegerField(required=False)
    dropoff_airline = forms.ModelChoiceField(
        queryset=Airline.objects.filter(is_active=True), required=False
    )
    dropoff_flight = forms.RegexField(
        regex=FLIGHT_RE,
        required=False,
        error_messages={"invalid": "Flight number must be digits (up to 6)."},
    )

    # Optional in-between stops (JSON array from the Alpine repeater).
    stops_json = forms.CharField(required=False)

    def _clean_airport(self, field: str):
        pk = self.cleaned_data.get(field)
        if pk is not None and not Airport.objects.filter(pk=pk).exists():
            raise forms.ValidationError("Unknown airport.")
        return pk

    def clean_pickup_airport(self):
        return self._clean_airport("pickup_airport")

    def clean_dropoff_airport(self):
        return self._clean_airport("dropoff_airport")

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
            flight = str(item.get("flight") or "").strip()
            if flight and not FLIGHT_RE.match(flight):
                raise forms.ValidationError("A stop's flight number must be digits (up to 6).")
            cleaned.append(
                {
                    "address": address,
                    "lat": _to_float(item.get("lat")),
                    "lng": _to_float(item.get("lng")),
                    **_flight_fields(
                        _to_int(item.get("airport")), _to_int(item.get("airline")), flight
                    ),
                }
            )
        airport_ids = {s["airport_id"] for s in cleaned if s["airport_id"]}
        known_airports = set(
            Airport.objects.filter(pk__in=airport_ids).values_list("pk", flat=True)
        )
        if airport_ids - known_airports:
            raise forms.ValidationError("A stop names an unknown airport.")
        airline_ids = {s["airline_id"] for s in cleaned if s["airline_id"]}
        active = set(
            Airline.objects.filter(pk__in=airline_ids, is_active=True).values_list("pk", flat=True)
        )
        if airline_ids - active:
            raise forms.ValidationError("A stop names an airline that isn't in the list.")
        return cleaned

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("company"):
            raise forms.ValidationError("spam detected")
        if not cleaned.get("email") and not cleaned.get("phone"):
            raise forms.ValidationError("Provide an email or phone so we can reach you.")

        trip_type = cleaned.get("trip_type") or Reservation.TripType.TRANSFER
        cleaned["trip_type"] = trip_type
        if trip_type == Reservation.TripType.HOURLY:
            if cleaned.get("hours") is None:
                self.add_error("hours", "Enter how many hours you need.")
        else:
            # A visitor can fill hours, toggle back to Transfer, and submit. Drop the
            # stale duration rather than recording it against a transfer.
            cleaned["hours"] = None

        # Assemble the ordered trip: pickup -> in-between stops -> drop-off, blanks removed.
        stops = []
        pickup = (cleaned.get("pickup") or "").strip()
        if pickup:
            airline = cleaned.get("pickup_airline")
            stops.append(
                {
                    "address": pickup[:ADDRESS_MAXLEN],
                    "lat": cleaned.get("pickup_lat"),
                    "lng": cleaned.get("pickup_lng"),
                    **_flight_fields(
                        cleaned.get("pickup_airport"),
                        airline.pk if airline else None,
                        cleaned.get("pickup_flight"),
                    ),
                }
            )
        stops.extend(cleaned.get("stops_json") or [])
        dropoff = (cleaned.get("dropoff") or "").strip()
        if dropoff:
            airline = cleaned.get("dropoff_airline")
            stops.append(
                {
                    "address": dropoff[:ADDRESS_MAXLEN],
                    "lat": cleaned.get("dropoff_lat"),
                    "lng": cleaned.get("dropoff_lng"),
                    **_flight_fields(
                        cleaned.get("dropoff_airport"),
                        airline.pk if airline else None,
                        cleaned.get("dropoff_flight"),
                    ),
                }
            )
        cleaned["stops"] = stops
        return cleaned
