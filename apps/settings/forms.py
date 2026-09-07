"""Model forms for the Settings screens."""

from django import forms

from apps.addresses.models import Venue
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
            "group_transport",
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
            "group_transport": (
                "Weddings and other group jobs size their runs from these. "
                "Turn it off for limousines and anything else that is not a shuttle."
            ),
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


_NUM = {"class": "field w-full", "min": 0}
_AREA = {"class": "field w-full", "rows": 3}


class DispatchAlertConfigForm(forms.ModelForm):
    """The single Dispatch-alerts settings screen (APC-23) — one row, no list."""

    class Meta:
        from apps.dispatch.models import DispatchAlertConfig

        model = DispatchAlertConfig
        fields = [
            "enabled",
            "unassigned_warn_hours",
            "unassigned_critical_hours",
            "otw_warn_minutes",
            "otw_critical_minutes",
            "arrived_warn_minutes",
            "arrived_critical_minutes",
            "driver_info_warn_hours",
            "driver_info_critical_hours",
            "affiliate_unacked_warn_hours",
            "alert_emails",
            "critical_sms",
        ]
        widgets = {
            "unassigned_warn_hours": forms.NumberInput(attrs=_NUM),
            "unassigned_critical_hours": forms.NumberInput(attrs=_NUM),
            "otw_warn_minutes": forms.NumberInput(attrs=_NUM),
            "otw_critical_minutes": forms.NumberInput(attrs=_NUM),
            "arrived_warn_minutes": forms.NumberInput(attrs=_NUM),
            "arrived_critical_minutes": forms.NumberInput(attrs=_NUM),
            "driver_info_warn_hours": forms.NumberInput(attrs=_NUM),
            "driver_info_critical_hours": forms.NumberInput(attrs=_NUM),
            "affiliate_unacked_warn_hours": forms.NumberInput(attrs=_NUM),
            "alert_emails": forms.Textarea(attrs=_AREA),
            "critical_sms": forms.Textarea(attrs=_AREA),
        }


class PricingConfigForm(forms.ModelForm):
    """The default cost ratio (spec 2026-09-05). One row, no list."""

    class Meta:
        from apps.reservations.models import PricingConfig

        model = PricingConfig
        fields = ["default_cost_ratio_pct"]
        widgets = {
            "default_cost_ratio_pct": forms.NumberInput(
                attrs={"class": "field w-full", "min": 1, "max": 100, "step": "0.01"}
            )
        }
        labels = {"default_cost_ratio_pct": "Vendor keeps (%)"}
        help_texts = {
            "default_cost_ratio_pct": (
                "The affiliate's share of the sell price. 65% means a $1,000 vendor cost is "
                "quoted at $1,538.50, earning a 35% margin. A lower number means a higher "
                "price. Every trip can override this."
            )
        }

    def clean_default_cost_ratio_pct(self):
        """0 would divide by zero; over 100 would price below cost."""
        value = self.cleaned_data["default_cost_ratio_pct"]
        if value <= 0 or value > 100:
            raise forms.ValidationError("Enter a percentage between 1 and 100.")
        return value


class NotificationConfigForm(forms.ModelForm):
    """The reservation-lifecycle messaging switches (APC-18-22). One row, no list."""

    class Meta:
        from apps.messaging.models import NotificationConfig

        model = NotificationConfig
        fields = [
            "enabled",
            "wedding_final_details_enabled",
            "trip_confirm_customer_enabled",
            "trip_confirm_affiliate_enabled",
            "driver_released_enabled",
            "status_dispatched_enabled",
            "status_on_the_way_enabled",
            "status_arrived_enabled",
            "order_auth_expired_enabled",
            "order_confirmed_enabled",
            "order_cancelled_enabled",
        ]


class VenueForm(forms.ModelForm):
    """A point of interest — venue, hotel or ceremony site — and what it can take.

    `max_vehicle` is the whole reason this screen exists: a place's limit is a vehicle
    ("nothing bigger than a Minibus gets down our drive"), not a number someone has to
    remember the seat count for. Left blank it means no limit of its own, and the picker
    labels that option with our largest coach so the default reads as what it does.
    """

    class Meta:
        model = Venue
        fields = [
            "name",
            "kind",
            "address",
            "city",
            "state",
            "max_vehicle",
            "cap_note",
            "access_note",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field w-full"}),
            # Tom Select, never a bare <select> (CLAUDE.md) — `data-tom` is what
            # `initTomSelects()` in static/js/app.js enhances on page load.
            "kind": forms.Select(attrs={"class": "field", "data-tom": "", "data-search": "off"}),
            "max_vehicle": forms.Select(
                attrs={"class": "field", "data-tom": "", "data-search": "off"}
            ),
            "address": forms.TextInput(attrs={"class": "field w-full"}),
            "city": forms.TextInput(attrs={"class": "field w-full"}),
            "state": forms.TextInput(attrs={"class": "field w-full", "maxlength": 2}),
            "cap_note": forms.TextInput(attrs={"class": "field w-full"}),
            "access_note": forms.TextInput(attrs={"class": "field w-full"}),
        }
        help_texts = {
            "cap_note": "Why the limit exists — read by whoever quotes the run.",
            "access_note": "Anything a driver needs on arrival (gate code, entrance, dock).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only shuttles are meaningful ceilings, and the catalog is the source of both the
        # options and the default label — rename a vehicle in Settings and this follows.
        from apps.leads.services import group_fleet

        fleet = group_fleet()
        self.fields["max_vehicle"].queryset = VehicleType.objects.filter(
            pk__in=[v.pk for v in fleet]
        ).order_by("capacity", "sort_order")
        biggest = fleet[-1] if fleet else None
        self.fields["max_vehicle"].empty_label = (
            f"{biggest.name} ({biggest.capacity} passengers) — no limit" if biggest else "No limit"
        )
        self.fields["max_vehicle"].label = "Largest vehicle that fits"
        self.fields["max_vehicle"].help_text = (
            "Defaults to our largest coach. Pick a smaller vehicle when the site cannot take one."
        )
