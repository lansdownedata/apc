"""Model forms for the fleet screens. Widgets mirror apps.vendors.forms."""

from django import forms
from django.db.models import Q

from apps.leads.models import VehicleType

from .models import Driver, Renewal, RenewalType, Vehicle

_TEXT = {"class": "field w-full"}
_DATE = {"class": "field w-full", "type": "date"}
_SELECT = {"class": "field w-full", "data-tom": "", "data-search": "off"}
_AREA = {"class": "field w-full", "rows": 3}


class DriverForm(forms.ModelForm):
    """`driver_number` is editable=False on the model, so it never appears here."""

    class Meta:
        model = Driver
        fields = ["name", "phone", "email", "status", "notes"]
        widgets = {
            "name": forms.TextInput(attrs=_TEXT),
            "phone": forms.TextInput(attrs=_TEXT),
            "email": forms.EmailInput(attrs=_TEXT),
            "status": forms.Select(attrs=_SELECT),
            "notes": forms.Textarea(attrs=_AREA),
        }


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = [
            "name",
            "vehicle_type",
            "year",
            "make",
            "model_name",
            "color",
            "license_plate",
            "vin",
            "status",
            "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_TEXT),
            "vehicle_type": forms.Select(attrs={"class": "field w-full", "data-tom": ""}),
            "year": forms.NumberInput(attrs={"class": "field w-full", "min": 1980, "max": 2100}),
            "make": forms.TextInput(attrs=_TEXT),
            "model_name": forms.TextInput(attrs=_TEXT),
            "color": forms.TextInput(attrs=_TEXT),
            "license_plate": forms.TextInput(attrs=_TEXT),
            "vin": forms.TextInput(attrs=_TEXT),
            "status": forms.Select(attrs=_SELECT),
            "notes": forms.Textarea(attrs=_AREA),
        }
        help_texts = {
            "vehicle_type": (
                "The rate-card class this unit runs as — drives vehicle fit in dispatch."
            ),
            "name": 'How dispatch refers to it, e.g. "Unit 1 – Black Suburban".',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keep = Q(active=True)
        if self.instance.pk and self.instance.vehicle_type_id:
            keep |= Q(pk=self.instance.vehicle_type_id)
        self.fields["vehicle_type"].queryset = VehicleType.objects.filter(keep)
        self.fields["vehicle_type"].empty_label = "Select a class…"


class RenewalForm(forms.ModelForm):
    """The subject (driver or vehicle) is fixed by the URL, never posted; `applies_to`
    narrows the type picker so a driver can't be handed a vehicle-only type."""

    class Meta:
        model = Renewal
        fields = ["renewal_type", "reference", "issued_on", "expires_on", "document", "notes"]
        widgets = {
            "renewal_type": forms.Select(attrs=_SELECT),
            "reference": forms.TextInput(attrs=_TEXT),
            "issued_on": forms.DateInput(attrs=_DATE),
            "expires_on": forms.DateInput(attrs=_DATE),
            "document": forms.FileInput(attrs={"class": "field w-full", "accept": ".pdf,image/*"}),
            "notes": forms.Textarea(attrs={"class": "field w-full", "rows": 2}),
        }
        labels = {"reference": "Number / reference", "document": "Scan (optional)"}

    def __init__(self, *args, applies_to: str, keep_type_id: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        keep = Q(active=True)
        if keep_type_id is not None:
            keep |= Q(pk=keep_type_id)
        if self.instance.pk:
            keep |= Q(pk=self.instance.renewal_type_id)
        self.fields["renewal_type"].queryset = RenewalType.objects.filter(
            applies_to=applies_to
        ).filter(keep)
        self.fields["renewal_type"].empty_label = "Select a type…"

    def clean(self):
        cleaned = super().clean()
        issued_on = cleaned.get("issued_on")
        expires_on = cleaned.get("expires_on")
        if issued_on and expires_on and expires_on < issued_on:
            raise forms.ValidationError("Expiry can't be before the issue date.")
        return cleaned


class RenewalTypeForm(forms.ModelForm):
    class Meta:
        model = RenewalType
        fields = ["name", "applies_to", "sort_order", "active"]
        widgets = {
            "name": forms.TextInput(attrs=_TEXT),
            "applies_to": forms.Select(attrs=_SELECT),
            "sort_order": forms.NumberInput(attrs={"class": "field w-full", "min": 0}),
        }
        help_texts = {"sort_order": "Lower numbers appear first."}
