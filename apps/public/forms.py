from django import forms

# Static service-type options for the booking widget's dropdown (no ServiceType
# model yet — matches the "static list of service types" scope in the plan brief).
# Reservation.service is a free-text CharField, so the option value IS the label —
# staff see "Wedding Transportation" on the reservation, not an internal slug.
SERVICE_TYPE_CHOICES = [
    ("Airport Transfer", "Airport Transfer"),
    ("Corporate Travel", "Corporate Travel"),
    ("Wedding Transportation", "Wedding Transportation"),
    ("Special Event", "Special Event"),
    ("Hourly Charter", "Hourly Charter"),
    ("Group / Shuttle Service", "Group / Shuttle Service"),
    ("Other", "Other"),
]


class BookingRequestForm(forms.Form):
    """Public booking request — a lead comes in through this, no auth required.

    The `company` field is a honeypot: real visitors never see or fill it
    (visually hidden in the template); bots that autofill every field trip it.
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

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("company"):
            raise forms.ValidationError("spam detected")
        if not cleaned.get("email") and not cleaned.get("phone"):
            raise forms.ValidationError("Provide an email or phone so we can reach you.")
        return cleaned
