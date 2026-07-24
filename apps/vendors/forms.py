"""Model forms for vendor management."""

from django import forms

from apps.core.phone import to_e164

from .models import Vendor, VendorDocument, VendorDriver, VendorInsurance

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


_DATE = {"class": "field w-full", "type": "date"}


class VendorDriverForm(forms.ModelForm):
    class Meta:
        model = VendorDriver
        fields = ["name", "phone", "email", "license_number", "active", "notes"]
        widgets = {
            "name": forms.TextInput(attrs=_TEXT),
            "phone": forms.TextInput(attrs=_TEXT),
            "email": forms.EmailInput(attrs=_TEXT),
            "license_number": forms.TextInput(attrs=_TEXT),
            "notes": forms.Textarea(attrs={"class": "field w-full", "rows": 2}),
        }


class VendorInsuranceForm(forms.ModelForm):
    class Meta:
        model = VendorInsurance
        fields = [
            "insurer",
            "policy_number",
            "coverage_amount",
            "effective_date",
            "expiry_date",
            "certificate",
            "notes",
        ]
        widgets = {
            "insurer": forms.TextInput(attrs=_TEXT),
            "policy_number": forms.TextInput(attrs=_TEXT),
            "coverage_amount": forms.NumberInput(attrs={"class": "field w-full", "min": 0}),
            "effective_date": forms.DateInput(attrs=_DATE),
            "expiry_date": forms.DateInput(attrs=_DATE),
            "certificate": forms.FileInput(
                attrs={"class": "field w-full", "accept": ".pdf,image/*"}
            ),
            "notes": forms.Textarea(attrs={"class": "field w-full", "rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        effective_date = cleaned.get("effective_date")
        expiry_date = cleaned.get("expiry_date")
        if effective_date and expiry_date and expiry_date < effective_date:
            raise forms.ValidationError("Expiry date can't be before the effective date.")
        return cleaned


class VendorDocumentForm(forms.ModelForm):
    class Meta:
        model = VendorDocument
        fields = ["label", "file"]
        widgets = {
            "label": forms.TextInput(attrs=_TEXT),
            "file": forms.FileInput(attrs={"class": "field w-full"}),
        }
