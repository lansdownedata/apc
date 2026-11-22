"""Forms for the customer's own sign-up, password and request-a-trip pages."""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.conf import settings
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from apps.accounts.forms import FIELD_CLASS
from apps.addresses.models import Airport
from apps.contacts.models import Contact
from apps.leads.models import ServiceType, VehicleType
from apps.public.forms import ADDRESS_MAXLEN, MAX_STOPS, BookingRequestForm
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.models import Passenger, Reservation

from .services import CustomerError, clean_mobile, clean_name, password_validators

NAME_MAX = Contact._meta.get_field("name").max_length
MOBILE_MAX = 40


class NameField(forms.CharField):
    """A person's name (`services.clean_name`: no control characters, single spaces)."""

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("error_messages", {"required": "Enter your name."})
        super().__init__(max_length=NAME_MAX, **kwargs)

    def to_python(self, value: object) -> str:
        return clean_name(super().to_python(value))


class MobileField(forms.CharField):
    """A mobile number, cleaned to E.164 (`services.clean_mobile`): the one rule every
    customer form holds a number to. Blank is allowed unless `required`."""

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("required", False)
        super().__init__(max_length=MOBILE_MAX, **kwargs)

    def clean(self, value: object) -> str:
        value = super().clean(value)
        try:
            return clean_mobile(value)
        except CustomerError as error:
            raise ValidationError(str(error)) from None


class SignupForm(forms.Form):
    """Step 1 of sign-up. The mobile is optional; when given it must be a real number."""

    name = NameField()
    email = forms.EmailField(max_length=254)
    phone = MobileField()


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


class DetailsForm(forms.Form):
    """The Account page's name and mobile. `on_file` is the mobile the page was drawn
    with. A mobile posted back as it was drawn (the same digits) is not an edit: it cleans
    to blank, which leaves the number on file (`services.update_details`), so a stored
    number that can't be dialled never blocks a change of name. A blank mobile does the
    same; only a number the customer changed is validated."""

    name = NameField()
    mobile = forms.CharField(max_length=MOBILE_MAX, required=False)

    def __init__(self, *args: object, on_file: str = "", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.on_file = on_file

    def clean_mobile(self) -> str:
        mobile = self.cleaned_data.get("mobile", "").strip()
        if _digits(mobile) == _digits(self.on_file):
            return ""
        try:
            return clean_mobile(mobile)
        except CustomerError as error:
            raise ValidationError(str(error)) from None


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
                validate_password(password, user, password_validators=password_validators())
            except ValidationError as error:
                self.add_error("new_password1", error)


class ChangePasswordForm(CustomerPasswordForm):
    """The Account page's password card: the current password, then the new one twice,
    held to the same rules as the set-password screens (judged against the account)."""

    current_password = forms.CharField(strip=False, required=False, widget=forms.PasswordInput)


def request_occasions() -> QuerySet[ServiceType]:
    """The website's occasions without the wedding one (name compared trimmed and
    case-folded, as `selectors.visible_trips` does): a wedding order is hidden from the
    customer's own portal, so this form can never make one."""
    return (
        ServiceType.objects.filter(active=True)
        .alias(folded=Lower(Trim("name")))
        .exclude(folded=WEDDING_SERVICE_NAME.lower())
    )


RIDERS = (("me", "Me"), ("other", "Someone else"))
# The fields of one row of the website's stops repeater (`bookingStops()` in app.js).
STOP_ROW_FIELDS = ("address", "lat", "lng", "display", "airport", "airline", "flight", "direction")


class TripRequestForm(BookingRequestForm):
    """The website booking form's trip fields for a signed-in customer. Its address,
    flight, stop and hourly cleaning are inherited untouched; its contact fields and
    honeypot are removed, since the account is the contact.

    Stricter than the website on what a request needs: a date and time still ahead where
    the pickup is (`pickup_zone`), and a pickup address. Adds who is riding, a vehicle
    preference and "Your reference".

    `needs_mobile` is for a customer with no mobile on file that can be dialled: riding
    themselves ("Me"), they are asked for one (`my_mobile`), because a trip's lead passenger
    is who the driver and the trip-day texts reach, and must have a number."""

    name = None
    email = None
    phone = None
    company = None

    pickup_date = forms.DateField(error_messages={"required": "Pick a date."})
    pickup_time = forms.TimeField(error_messages={"required": "Pick a time."})
    pickup = forms.CharField(
        max_length=ADDRESS_MAXLEN, error_messages={"required": "Enter a pickup address."}
    )
    service_type = forms.ModelChoiceField(queryset=ServiceType.objects.none(), required=False)

    rider = forms.ChoiceField(choices=RIDERS, required=False, initial="me")
    rider_name = forms.CharField(
        max_length=Passenger._meta.get_field("name").max_length, required=False
    )
    rider_mobile = forms.CharField(max_length=MOBILE_MAX, required=False)
    my_mobile = forms.CharField(max_length=MOBILE_MAX, required=False)
    vehicle_preference = forms.ModelChoiceField(queryset=VehicleType.objects.none(), required=False)
    customer_reference = forms.CharField(
        max_length=Reservation._meta.get_field("customer_reference").max_length, required=False
    )

    def __init__(self, *args: object, needs_mobile: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.needs_mobile = needs_mobile
        # Read per form, not at import, so a Settings edit shows without a redeploy.
        self.fields["service_type"].queryset = request_occasions()
        self.fields["vehicle_preference"].queryset = VehicleType.objects.filter(active=True)

    def occasion_options(self) -> list[tuple[str, str]]:
        rows = self.fields["service_type"].queryset.values_list("pk", "name")
        return [(str(pk), name) for pk, name in rows]

    def vehicle_options(self) -> list[tuple[str, str]]:
        rows = self.fields["vehicle_preference"].queryset.values_list("pk", "name")
        return [(str(pk), name) for pk, name in rows]

    def posted_stops(self) -> list[dict[str, str]]:
        """The extra stops as posted, in the repeater's own row shape (every value text),
        so a re-rendered form puts them back as the customer left them. Anything that
        isn't a list of rows is dropped; the form's errors already say so."""
        try:
            rows = json.loads(self.data.get("stops_json") or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(rows, list):
            return []
        return [
            {key: "" if row.get(key) is None else str(row[key]) for key in STOP_ROW_FIELDS}
            for row in rows[:MAX_STOPS]
            if isinstance(row, dict)
        ]

    def _mobile(self, cleaned: dict, field: str, missing: str) -> None:
        """Hold `field` to the one mobile rule, required here: cleaned to E.164 in place,
        or an error beside it."""
        try:
            cleaned[field] = clean_mobile(cleaned.get(field))
        except CustomerError as error:
            self.add_error(field, str(error))
            return
        if not cleaned[field]:
            self.add_error(field, missing)

    def _not_in_the_past(self, cleaned: dict) -> None:
        """The pickup must still be ahead on the clock where the pickup is, not on the
        office's: 11 PM tonight in Los Angeles is not yesterday because it is already
        tomorrow in Virginia."""
        day, clock = cleaned.get("pickup_date"), cleaned.get("pickup_time")
        if day is None:
            return
        now = timezone.now().astimezone(pickup_zone(cleaned.get("stops") or []))
        if day < now.date():
            self.add_error("pickup_date", "Pick today or a later date.")
        elif clock is not None and datetime.combine(day, clock, tzinfo=now.tzinfo) <= now:
            self.add_error("pickup_time", "Pick a time later today, or a later date.")

    def clean(self) -> dict:
        cleaned = super().clean()
        self._not_in_the_past(cleaned)
        cleaned["rider"] = cleaned.get("rider") or "me"
        if cleaned["rider"] != "other":
            cleaned["rider_name"], cleaned["rider_mobile"] = "", ""
            if self.needs_mobile:
                self._mobile(
                    cleaned, "my_mobile", "Enter your mobile, so your driver can reach you."
                )
            else:
                cleaned["my_mobile"] = ""
            return cleaned
        cleaned["my_mobile"] = ""
        if not cleaned.get("rider_name"):
            self.add_error("rider_name", "Enter the passenger's name.")
        self._mobile(cleaned, "rider_mobile", "Enter the passenger's mobile.")
        return cleaned


def pickup_zone(stops: list[dict]) -> ZoneInfo:
    """The time zone at a request's pickup, as the trip will get it
    (`reservations.timezones.resolve`): the pickup airport's, else the zone at the picked
    address's coordinates, else the business zone (an address typed without picking a
    search result has no coordinates)."""
    from apps.reservations import timezones

    name = ""
    if stops:
        pickup = stops[0]
        if pickup.get("airport_id"):
            name = (
                Airport.objects.filter(pk=pickup["airport_id"])
                .values_list("timezone", flat=True)
                .first()
                or ""
            )
        name = name or timezones.timezone_at(pickup.get("lat"), pickup.get("lng")) or ""
    try:
        return ZoneInfo(name or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)
