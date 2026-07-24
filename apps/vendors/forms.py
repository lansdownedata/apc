"""Model forms for vendor management."""

from django import forms

from apps.core.phone import to_e164

from .models import Vendor

_TEXT = {"class": "field w-full"}


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = [
            "name",
            "contact_name",
            "phone",
            "email",
            "status",
            "service_area",
            "location_name",
            "address_line1",
            "unit",
            "city",
            "state",
            "postal_code",
            "website",
            "usdot_number",
            "vehicle_types",
            "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_TEXT),
            "contact_name": forms.TextInput(attrs=_TEXT),
            "phone": forms.TextInput(attrs=_TEXT),
            "email": forms.EmailInput(attrs=_TEXT),
            "status": forms.Select(
                attrs={"class": "field w-full", "data-tom": "", "data-search": "off"}
            ),
            "service_area": forms.TextInput(attrs=_TEXT),
            "location_name": forms.TextInput(attrs=_TEXT),
            "address_line1": forms.TextInput(attrs=_TEXT),
            "unit": forms.TextInput(attrs=_TEXT),
            "city": forms.TextInput(attrs=_TEXT),
            "state": forms.TextInput(attrs=_TEXT),
            "postal_code": forms.TextInput(attrs=_TEXT),
            "website": forms.URLInput(attrs=_TEXT),
            "usdot_number": forms.TextInput(attrs=_TEXT),
            "vehicle_types": forms.SelectMultiple(
                attrs={"class": "field w-full", "data-tom": "", "multiple": "multiple"}
            ),
            "notes": forms.Textarea(attrs={"class": "field w-full", "rows": 3}),
        }

    def clean_phone(self) -> str:
        raw = (self.cleaned_data.get("phone") or "").strip()
        return to_e164(raw) or raw
