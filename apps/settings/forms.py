"""Model forms for the Settings screens."""

from django import forms

from apps.leads.models import ServiceType, VehicleType


class VehicleTypeForm(forms.ModelForm):
    """The project's first ModelForm with a file field — the hand-written-input
    approach used elsewhere can't carry enctype cleanly."""

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
            "hourly_min_hours": (
                "Minimum billable hours for hourly trips "
                "(0 = every hourly trip needs override hours)."
            ),
            "transfer_min_hours": (
                "Minimum billable hours for transfers (1 = the rate is the flat price)."
            ),
            "sort_order": "Lower numbers appear first.",
        }


class ServiceTypeForm(forms.ModelForm):
    class Meta:
        model = ServiceType
        fields = ["name", "sort_order", "active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field w-full"}),
            "sort_order": forms.NumberInput(attrs={"class": "field w-full", "min": 0}),
        }
        help_texts = {
            "sort_order": "Lower numbers appear first.",
            "active": "Inactive types stay on the trips that use them, but aren't offered.",
        }

    def validate_unique(self) -> None:
        """Surface the case-insensitive name constraint as a form error.

        ModelForm only checks constraints it can map to fields; the UniqueConstraint is on
        Lower("name"), so without this the duplicate reaches the database and 500s.
        """
        super().validate_unique()
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            return
        clash = ServiceType.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            self.add_error("name", "A service type with that name already exists.")
