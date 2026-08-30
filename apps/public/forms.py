import json
import re
from datetime import time

from django import forms

from apps.addresses.models import PRIVATE_AIRLINE_IATA, Airline, Airport, Venue
from apps.leads.models import ServiceType
from apps.reservations.models import Reservation

from .wedding import GROUPS, MAX_LEGS, Site, vehicle_for
from .wedding import MAX_PASSENGERS as MAX_WEDDING_PASSENGERS
from .wedding import MIN_PASSENGERS as MIN_WEDDING_PASSENGERS


def occasion_options() -> list[tuple[str, str]]:
    """The booking widget's "occasion" dropdown, from the Settings catalog.

    Same list the reservation editor offers, so the office and the website can't drift
    apart. Evaluated per request, not at import, or edits in Settings would need a
    redeploy to show up. Trip *type* (transfer/hourly) is a separate field posting
    Reservation.trip_type — these are the occasions that used to be conflated with it.
    """
    rows = ServiceType.objects.filter(active=True).values_list("pk", "name")
    return [(str(pk), name) for pk, name in rows]


MAX_STOPS = 4
# Two or three room blocks is normal; past that it is a bot or a paste accident.
MAX_HOTELS = 6
ADDRESS_MAXLEN = 255
FLIGHT_RE = re.compile(r"^\d{1,6}$")
# A US-registered tail number: "N" + up to 5 alphanumerics (e.g. "N561FX") — mirrors
# apps.reservations.drafts.TAIL_RE for the seeded Private carrier (2026-08-29 §2).
TAIL_RE = re.compile(r"^N[0-9A-Z]{1,5}$")


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


def _valid_flight_number(value: str, *, is_private: bool) -> str:
    """Same shape rule as apps.reservations.drafts: digits only for a real carrier, an
    N-prefixed tail number (case-insensitive on input, stored upper-case) for the seeded
    Private one. `value` is already trimmed; blank is returned as-is (nothing to validate)."""
    if not value:
        return value
    if is_private:
        text = value.upper()
        if not TAIL_RE.match(text):
            raise forms.ValidationError("Enter a tail number starting with N (up to 6 characters).")
        return text
    if not FLIGHT_RE.match(value):
        raise forms.ValidationError("Flight number must be digits (up to 6).")
    return value


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
    service_type = forms.ModelChoiceField(
        queryset=ServiceType.objects.filter(active=True), required=False
    )
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
    pickup_flight = forms.CharField(max_length=6, required=False)
    dropoff_airport = forms.IntegerField(required=False)
    dropoff_airline = forms.ModelChoiceField(
        queryset=Airline.objects.filter(is_active=True), required=False
    )
    dropoff_flight = forms.CharField(max_length=6, required=False)

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

    def _clean_flight(self, end: str) -> str:
        """Digits only for a real carrier, a tail number for the seeded Private one — and
        only meaningful with an airport; without one the value is dropped rather than
        rejected, so a stale entry in a hidden row can't block the form. `{end}_airline` is
        declared earlier on the form than `{end}_flight`, so its cleaned (model) value is
        already in `cleaned_data` by the time this runs."""
        flight = (self.cleaned_data.get(f"{end}_flight") or "").strip()
        if not self.cleaned_data.get(f"{end}_airport"):
            return ""
        airline = self.cleaned_data.get(f"{end}_airline")
        return _valid_flight_number(flight, is_private=bool(airline and airline.is_private))

    def clean_pickup_flight(self):
        return self._clean_flight("pickup")

    def clean_dropoff_flight(self):
        return self._clean_flight("dropoff")

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
        # Lazy + cached: the seeded Private carrier's pk, queried at most once and only if
        # some stop actually carries flight info (most bookings have none in stops_json at
        # all — `test_blank_address_stops_dropped` runs with no `db` fixture on purpose).
        private_id_cache: list[int | None] = []

        def private_airline_id() -> int | None:
            if not private_id_cache:
                private_id_cache.append(
                    Airline.objects.filter(iata=PRIVATE_AIRLINE_IATA)
                    .values_list("pk", flat=True)
                    .first()
                )
            return private_id_cache[0]

        cleaned = []
        for item in data:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or "").strip()[:ADDRESS_MAXLEN]
            if not address:
                continue
            airport_id = _to_int(item.get("airport"))
            airline_id = _to_int(item.get("airline"))
            flight = str(item.get("flight") or "").strip()
            if airport_id and flight:
                flight = _valid_flight_number(
                    flight, is_private=airline_id is not None and airline_id == private_airline_id()
                )
            direction = str(item.get("direction") or "").strip().lower()
            if direction not in ("", "arrival", "departure"):
                raise forms.ValidationError("A stop's flight must be arriving or departing.")
            cleaned.append(
                {
                    "address": address,
                    "lat": _to_float(item.get("lat")),
                    "lng": _to_float(item.get("lng")),
                    **_flight_fields(airport_id, airline_id, flight),
                    "flight_direction": direction if airport_id else "",
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


def _required_text(value, maxlen: int, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise forms.ValidationError(message)
    return text[:maxlen]


def _leg_time(value) -> time:
    """ "HH:MM" off the timeline's <input type="time">, and nothing else."""
    try:
        hour, _, minute = str(value or "").partition(":")
        return time(int(hour), int(minute))
    except (TypeError, ValueError) as e:
        raise forms.ValidationError("A movement has a pickup time we can't read.") from e


def _leg_passengers(value) -> int:
    try:
        pax = int(value)
    except (TypeError, ValueError) as e:
        raise forms.ValidationError("A movement has a passenger count we can't read.") from e
    if not MIN_WEDDING_PASSENGERS <= pax <= MAX_WEDDING_PASSENGERS:
        raise forms.ValidationError(
            f"A movement needs between {MIN_WEDDING_PASSENGERS} and "
            f"{MAX_WEDDING_PASSENGERS} passengers."
        )
    return pax


def _site(venue: Venue | None, typed_name: str | None) -> Site | None:
    """A directory row when the couple picked one, else whatever they typed.

    A typed site is not second-class — the office resolves the address by hand and the
    trip is otherwise identical — so it keeps its name rather than being dropped.
    """
    if venue is not None:
        return Site(
            name=venue.name,
            sub=venue.location_line,
            city=venue.city,
            state=venue.state,
            address=venue.address,
            latitude=str(venue.latitude) if venue.latitude is not None else None,
            longitude=str(venue.longitude) if venue.longitude is not None else None,
            vehicle_cap=venue.vehicle_cap,
            cap_note=venue.cap_note,
            venue_id=venue.pk,
        )
    name = (typed_name or "").strip()
    return Site(name=name[:255]) if name else None


class WeddingRequestForm(forms.Form):
    """The wedding intake's single POST (spec 2026-08-30 §6.2).

    Same contract as `BookingRequestForm` — plain form, honeypot, one of email/phone —
    but the payload describes an *event*, and `legs_json` is the itinerary the customer
    edited rather than the one we generated. Nothing in it is trusted: shape, count and
    headcount are checked here, and the vehicle recommendation is re-derived from our
    own rule (a smaller coach is never a customer's decision to make).
    """

    name = forms.CharField(max_length=200)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=32, required=False)

    wedding_date = forms.DateField()
    venue_id = forms.IntegerField(required=False)
    venue_name = forms.CharField(max_length=255)
    ceremony_venue_id = forms.IntegerField(required=False)
    ceremony_venue_name = forms.CharField(max_length=255, required=False)
    same_site = forms.BooleanField(required=False, initial=True)

    groups = forms.CharField()
    guest_count = forms.IntegerField(min_value=1, max_value=MAX_WEDDING_PASSENGERS, required=False)
    party_count = forms.IntegerField(min_value=1, max_value=MAX_WEDDING_PASSENGERS, required=False)
    family_count = forms.IntegerField(min_value=1, max_value=MAX_WEDDING_PASSENGERS, required=False)

    hotels_json = forms.CharField(required=False)
    hotels_tbd = forms.BooleanField(required=False)

    ceremony_time = forms.TimeField(required=False)
    end_time = forms.TimeField(required=False)
    times_tbd = forms.BooleanField(required=False)

    legs_json = forms.CharField()
    company = forms.CharField(required=False)  # honeypot — bots fill it

    def _venue(self, field: str) -> Venue | None:
        """A directory row for the posted id, or None when the couple typed their own.

        An id we don't recognise is an error rather than a silent fallback: it means the
        payload was tampered with or the row was retired mid-flow, and either way the
        address on the trip would be wrong.
        """
        pk = self.cleaned_data.get(field)
        if not pk:
            return None
        venue = Venue.objects.filter(pk=pk, is_active=True).first()
        if venue is None:
            raise forms.ValidationError("We don't recognise that venue — search for it again.")
        return venue

    def clean_venue_id(self):
        return self.cleaned_data.get("venue_id")

    def clean_groups(self) -> list[str]:
        raw = self.cleaned_data.get("groups") or ""
        chosen = {g.strip().lower() for g in raw.split(",") if g.strip()}
        # Canonical order, not the order the tiles happened to be clicked in, so the
        # notes and the itinerary read the same for every customer.
        groups = [g for g in GROUPS if g in chosen]
        if not groups:
            raise forms.ValidationError("Tell us who needs a ride.")
        return groups

    def clean_hotels_json(self) -> list[dict]:
        raw = (self.cleaned_data.get("hotels_json") or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise forms.ValidationError("Could not read the hotel list.") from e
        if not isinstance(data, list):
            raise forms.ValidationError("Hotels must be a list.")
        return [
            h for h in data[:MAX_HOTELS] if isinstance(h, dict) and (h.get("name") or "").strip()
        ]

    def clean_legs_json(self) -> list[dict]:
        raw = (self.cleaned_data.get("legs_json") or "").strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise forms.ValidationError("Could not read the itinerary.") from e
        if not isinstance(data, list) or not data:
            raise forms.ValidationError("Add at least one movement to your day.")
        if len(data) > MAX_LEGS:
            raise forms.ValidationError(
                f"That's more movements than we can quote in one go (max {MAX_LEGS})."
            )
        legs = []
        for item in data:
            if not isinstance(item, dict):
                raise forms.ValidationError("That itinerary isn't in a shape we can read.")
            legs.append(
                {
                    "id": str(item.get("id") or "")[:40],
                    "time": _leg_time(item.get("time")),
                    "title": _required_text(item.get("title"), 160, "Every movement needs a name."),
                    "from": _required_text(item.get("from"), 255, "Every movement needs a pickup."),
                    "from_sub": str(item.get("from_sub") or "").strip()[:255],
                    "to": _required_text(
                        item.get("to"), 255, "Every movement needs a destination."
                    ),
                    "to_sub": str(item.get("to_sub") or "").strip()[:255],
                    "pax": _leg_passengers(item.get("pax")),
                    "optional": bool(item.get("optional")),
                }
            )
        legs.sort(key=lambda leg: leg["time"])
        return legs

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("company"):
            raise forms.ValidationError("spam detected")
        if not cleaned.get("email") and not cleaned.get("phone"):
            raise forms.ValidationError("Provide an email or phone so we can reach you.")
        return self.resolve_wedding(cleaned)

    def resolve_wedding(self, cleaned: dict) -> dict:
        """Turn posted ids into Sites and re-derive every leg's vehicle.

        Split out from `clean()` so the portal's subclass can reuse it without inheriting
        the honeypot and the email-or-phone rule, neither of which applies behind auth.
        This is the half that must never fork between the website and the office.
        """
        venue = self._venue("venue_id")
        ceremony_venue = self._venue("ceremony_venue_id")
        cleaned["venue"] = _site(venue, cleaned.get("venue_name"))
        cleaned["ceremony"] = (
            None
            if cleaned.get("same_site")
            else _site(ceremony_venue, cleaned.get("ceremony_venue_name"))
        )

        hotel_ids = [h["venue_id"] for h in cleaned.get("hotels_json") or [] if h.get("venue_id")]
        known = {v.pk: v for v in Venue.objects.filter(pk__in=hotel_ids, is_active=True)}
        if set(hotel_ids) - set(known):
            self.add_error("hotels_json", "One of those hotels isn't one we recognise.")
            return cleaned
        cleaned["hotels"] = [
            _site(known.get(h.get("venue_id")), h.get("name"))
            for h in cleaned.get("hotels_json") or []
        ]

        # The recommendation is ours, not the browser's: re-derive every leg's vehicle
        # from our own rule and the venue's own cap, whatever the client posted.
        cap = venue.vehicle_cap if venue else None
        for leg in cleaned.get("legs_json") or []:
            leg["vehicle"] = vehicle_for(leg["pax"], cap)
        cleaned["legs"] = cleaned.get("legs_json") or []
        return cleaned


# The picker's card slugs mapped onto the Settings catalog by name. Slugs are URL-facing
# and permanent; catalog names are editable in Settings, so a rename there breaks the
# preselection rather than the page — the picker still works, it just opens the widget
# with no occasion chosen.
SERVICE_SLUGS = {
    "airport": "Airport Transfer",
    "corporate": "Corporate Travel",
    "wedding": "Wedding Transportation",
}


def service_type_for_slug(slug: str) -> ServiceType | None:
    """The active catalog entry a picker card preselects, or None for anything else."""
    name = SERVICE_SLUGS.get((slug or "").strip().lower())
    if not name:
        return None
    return ServiceType.objects.filter(name__iexact=name, active=True).first()


def service_slug_ids() -> dict[str, int | None]:
    """`{slug: pk}` for the hero picker's in-place swap (JSON-safe)."""
    return {
        slug: (st.pk if (st := service_type_for_slug(slug)) else None) for slug in SERVICE_SLUGS
    }
