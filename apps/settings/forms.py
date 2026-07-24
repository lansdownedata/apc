"""Model forms for the Settings screens."""

from decimal import Decimal
from typing import Any

from django import forms

from apps.leads.models import VehicleType

# Rate-card fields default to 0 on the model, but Django's ModelForm derives
# `required` from `field.blank` (not from the model default), so a plain
# ModelForm treats these as required. Make them optional here and fall back
# to 0 in cleaning so omitting them (e.g. legacy callers, partial forms)
# behaves like the model default rather than a validation error.
RATE_CARD_FIELDS = ("rate", "hourly_min_hours", "transfer_min_hours")


class VehicleTypeForm(forms.ModelForm):
    """The project's first ModelForm with a file field — the hand-written-input
    approach used elsewhere can't carry enctype cleanly."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in RATE_CARD_FIELDS:
            self.fields[name].required = False

    def clean_rate(self) -> Decimal:
        return self.cleaned_data.get("rate") or Decimal("0")

    def clean_hourly_min_hours(self) -> Decimal:
        return self.cleaned_data.get("hourly_min_hours") or Decimal("0")

    def clean_transfer_min_hours(self) -> Decimal:
        return self.cleaned_data.get("transfer_min_hours") or Decimal("0")

    class Meta:
        model = VehicleType
        fields = [
            "name",
            "capacity",
            "description",
            "image",
            "rate",
            "hourly_min_hours",
            "transfer_min_hours",
            "sort_order",
            "active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field w-full"}),
            "capacity": forms.NumberInput(attrs={"class": "field w-full", "min": 1}),
            "description": forms.Textarea(attrs={"class": "field w-full", "rows": 2}),
            "rate": forms.NumberInput(attrs={"class": "field w-full", "min": 0, "step": "0.01"}),
            "hourly_min_hours": forms.NumberInput(
                attrs={"class": "field w-full", "min": 0, "step": "0.25"}
            ),
            "transfer_min_hours": forms.NumberInput(
                attrs={"class": "field w-full", "min": 0, "step": "0.25"}
            ),
            "sort_order": forms.NumberInput(attrs={"class": "field w-full", "min": 0}),
            # Plain FileInput, not the default ClearableFileInput: the template wraps
            # it in a styled dropzone (imageUpload) and renders the current photo as a
            # preview, so Django's "Currently: … Clear" chrome would be redundant noise.
            "image": forms.FileInput(attrs={"class": "sr-only", "accept": "image/*"}),
        }
        help_texts = {
            "image": "Landscape photo, transparent or white background, about 1200px wide.",
            "description": "One line shown under the photo on the customer's quote page.",
            "rate": "Per-hour rate. Pre-fills onto a trip when this vehicle is chosen.",
            "hourly_min_hours": "Minimum billable hours for hourly trips (0 = none).",
            "transfer_min_hours": "Minimum billable hours for transfers (0 = none).",
            "sort_order": "Lower numbers appear first.",
        }
