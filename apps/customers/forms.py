"""Forms for the customer's own sign-up, password and request-a-trip pages."""

from __future__ import annotations

import json
from datetime import date

from django import forms
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from apps.accounts.forms import FIELD_CLASS, INVALID_PHONE
from apps.core.phone import to_e164
from apps.leads.models import ServiceType, VehicleType
from apps.public.forms import ADDRESS_MAXLEN, MAX_STOPS, BookingRequestForm
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.models import Passenger, Reservation

# The set-password screen promises "At least 10 characters."; the site-wide minimum is 8.
MIN_PASSWORD_LENGTH = 10


class SignupForm(forms.Form):
    """Step 1 of sign-up. The mobile is optional; when given it must be a real number."""

    name = forms.CharField(max_length=200)
    email = forms.EmailField(max_length=254)
    phone = forms.CharField(max_length=40, required=False)

    def clean_phone(self) -> str:
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and to_e164(phone) is None:
            raise ValidationError(INVALID_PHONE)
        return phone


class DetailsForm(forms.Form):
    """The Account page's name and mobile. The mobile may be blank, which leaves the number
    on file (`services.update_details`)."""

    name = forms.CharField(max_length=200, error_messages={"required": "Enter your name."})
    mobile = forms.CharField(max_length=40, required=False)

    def clean_mobile(self) -> str:
        mobile = self.cleaned_data.get("mobile", "").strip()
        if mobile and to_e164(mobile) is None:
            raise ValidationError(INVALID_PHONE)
        return mobile


def password_validators() -> list:
    """The site's password validators, with the customer screen's longer minimum."""
    rest = [
        v for v in get_default_password_validators() if not isinstance(v, MinimumLengthValidator)
    ]
    return [MinimumLengthValidator(MIN_PASSWORD_LENGTH), *rest]


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

    Stricter than the website on what a request needs: a date (today or later in the
    business time zone), a time and a pickup address. Adds who is riding, a vehicle
    preference and "Your reference"."""

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
    rider_mobile = forms.CharField(max_length=40, required=False)
    vehicle_preference = forms.ModelChoiceField(queryset=VehicleType.objects.none(), required=False)
    customer_reference = forms.CharField(
        max_length=Reservation._meta.get_field("customer_reference").max_length, required=False
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
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

    def clean_pickup_date(self) -> date | None:
        day = self.cleaned_data.get("pickup_date")
        if day is not None and day < timezone.localdate():
            raise ValidationError("Pick today or a later date.")
        return day

    def clean(self) -> dict:
        cleaned = super().clean()
        cleaned["rider"] = cleaned.get("rider") or "me"
        if cleaned["rider"] != "other":
            cleaned["rider_name"], cleaned["rider_mobile"] = "", ""
            return cleaned
        if not cleaned.get("rider_name"):
            self.add_error("rider_name", "Enter the passenger's name.")
        mobile = cleaned.get("rider_mobile") or ""
        if not mobile:
            self.add_error("rider_mobile", "Enter the passenger's mobile.")
        elif to_e164(mobile) is None:
            self.add_error("rider_mobile", INVALID_PHONE)
        else:
            cleaned["rider_mobile"] = to_e164(mobile)
        return cleaned
