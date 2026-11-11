"""Model forms for the Settings screens."""

import re
from decimal import Decimal

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.core.validators import validate_email
from django.db.models import Q

from apps.addresses.models import Venue
from apps.leads.models import ServiceType, VehicleType
from apps.messaging import editor, fields, recipients, triggers
from apps.messaging.models import MessageTemplate
from apps.tasks import schedule
from apps.vendors.models import AffiliateDocumentType

UNIT_LABELS = {"hours": "hours", "days": "days", "business_days": "business days"}


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
            "overtime_class",
            "cleaning_fee",
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
            # Tom Select, never a bare <select> (CLAUDE.md).
            "overtime_class": forms.Select(
                attrs={"class": "field w-full", "data-tom": "", "data-search": "off"}
            ),
            "cleaning_fee": forms.NumberInput(
                attrs={"class": "field w-full", "min": 0, "step": "0.01"}
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Left blank means no fee, the same as 0.
        self.fields["cleaning_fee"].required = False

    def clean_cleaning_fee(self) -> Decimal:
        return self.cleaned_data.get("cleaning_fee") or Decimal("0")


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

    DUPLICATE = "A service type with that name already exists."

    def validate_unique(self) -> None:
        """Surface the case-insensitive name constraint under Name, once.

        The UniqueConstraint is on Lower("name"), so Django's own constraint check reports a
        clash with no field — above the buttons. It is moved under Name here, where the
        problem is, rather than shown twice (SET-09).
        """
        super().validate_unique()
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            return
        clash = ServiceType.objects.filter(name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            general = self._errors.get(NON_FIELD_ERRORS)
            if general is not None:
                rest = [e for e in general if e != self.DUPLICATE]
                if rest:
                    self._errors[NON_FIELD_ERRORS] = self.error_class(rest, error_class="nonfield")
                else:
                    del self._errors[NON_FIELD_ERRORS]
            self.add_error("name", self.DUPLICATE)


_NUM = {"class": "field w-full", "min": 0}
_CHECK = {"class": "rounded focus-visible:ring-2 focus-visible:ring-gold disabled:opacity-50"}
_AREA = {"class": "field w-full", "rows": 3}
_TIME = {"class": "field w-full", "type": "time"}


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
            "require_driver_cell",
            "require_vehicle_desc",
            "require_vehicle_number",
            "driver_page_show_passenger_contact",
            "critical_sms",
        ]
        widgets = {
            "require_driver_cell": forms.CheckboxInput(attrs=_CHECK),
            "require_vehicle_desc": forms.CheckboxInput(attrs=_CHECK),
            "require_vehicle_number": forms.CheckboxInput(attrs=_CHECK),
            "unassigned_warn_hours": forms.NumberInput(attrs=_NUM),
            "unassigned_critical_hours": forms.NumberInput(attrs=_NUM),
            "otw_warn_minutes": forms.NumberInput(attrs=_NUM),
            "otw_critical_minutes": forms.NumberInput(attrs=_NUM),
            "arrived_warn_minutes": forms.NumberInput(attrs=_NUM),
            "arrived_critical_minutes": forms.NumberInput(attrs=_NUM),
            "driver_info_warn_hours": forms.NumberInput(attrs=_NUM),
            "driver_info_critical_hours": forms.NumberInput(attrs=_NUM),
            "affiliate_unacked_warn_hours": forms.NumberInput(attrs=_NUM),
            "critical_sms": forms.Textarea(attrs=_AREA),
            "driver_page_show_passenger_contact": forms.CheckboxInput(attrs=_CHECK),
        }
        labels = {
            "unassigned_warn_hours": "No coverage warning (hours)",
            "unassigned_critical_hours": "No coverage critical (hours)",
            "otw_warn_minutes": "Not en route warning (minutes)",
            "otw_critical_minutes": "Not en route critical (minutes)",
            "arrived_warn_minutes": "Not arrived warning (minutes)",
            "arrived_critical_minutes": "Not arrived critical (minutes)",
            "driver_info_warn_hours": "Driver info missing warning (hours)",
            "driver_info_critical_hours": "Driver info missing critical (hours)",
            "affiliate_unacked_warn_hours": "Affiliate unacknowledged warning (hours)",
            "critical_sms": "Critical SMS numbers",
            "driver_page_show_passenger_contact": "Show passenger info to vendor drivers",
        }
        help_texts = {
            "driver_page_show_passenger_contact": "Off: vendor drivers see passenger names and "
            "count, not the lead's mobile. Our own drivers always see it.",
        }


_OWNER = {"class": "field w-full", "data-tom": "", "data-placeholder": "No default owner"}


class TaskConfigForm(forms.ModelForm):
    """The single Tasks settings screen (APC-49) — department owners, escalation, digest."""

    class Meta:
        from apps.tasks.models import TaskConfig

        model = TaskConfig
        fields = [
            "enabled",
            "sales_owner",
            "operations_owner",
            "affiliate_mgmt_owner",
            "customer_service_owner",
            "accounting_owner",
            "overdue_grace_hours",
            "post_trip_grace_hours",
            "grace_large_minutes",
            "grace_standard_minutes",
            "transfer_grace_minutes",
            "airport_domestic_grace_minutes",
            "airport_international_grace_minutes",
            "overtime_block_minutes",
            "hold_enabled",
            "hold_place_time",
            "hold_lead_hours",
            "hold_release_days",
        ]
        widgets = {
            "sales_owner": forms.Select(attrs=_OWNER),
            "operations_owner": forms.Select(attrs=_OWNER),
            "affiliate_mgmt_owner": forms.Select(attrs=_OWNER),
            "customer_service_owner": forms.Select(attrs=_OWNER),
            "accounting_owner": forms.Select(attrs=_OWNER),
            "overdue_grace_hours": forms.NumberInput(attrs=_NUM),
            "post_trip_grace_hours": forms.NumberInput(attrs=_NUM),
            "grace_large_minutes": forms.NumberInput(attrs=_NUM),
            "grace_standard_minutes": forms.NumberInput(attrs=_NUM),
            "transfer_grace_minutes": forms.NumberInput(attrs=_NUM),
            "airport_domestic_grace_minutes": forms.NumberInput(attrs=_NUM),
            "airport_international_grace_minutes": forms.NumberInput(attrs=_NUM),
            "overtime_block_minutes": forms.NumberInput(attrs=_NUM),
            "hold_enabled": forms.CheckboxInput(attrs=_CHECK),
            # A native time input is fine here: it is not an option picker (CLAUDE.md).
            "hold_place_time": forms.TimeInput(attrs=_TIME, format="%H:%M"),
            "hold_lead_hours": forms.NumberInput(attrs=_NUM | {"max": 24}),
            "hold_release_days": forms.NumberInput(attrs=_NUM),
        }
        # Short, because the three sit side by side as labelled fields (SET-11).
        labels = {
            "hold_place_time": "Place the hold at",
            "hold_lead_hours": "Or hours before pickup",
            "hold_release_days": "Release within (days)",
        }
        help_texts = {
            "hold_place_time": (
                "On the day of the order's first held trip, in the trip's local time."
            ),
            "hold_lead_hours": "Whichever comes first.",
            "hold_release_days": "After the order's last held trip, at the latest.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User

        active = User.objects.filter(is_active=True).order_by("first_name", "username")
        for name in self.Meta.model.OWNER_FIELDS.values():
            self.fields[name].queryset = active
            self.fields[name].empty_label = "No default owner"


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
        self.fields[
            "max_vehicle"
        ].help_text = (
            "Defaults to our largest coach. Pick a smaller vehicle when the site cannot take one."
        )


class ChecklistScheduleForm(forms.Form):
    """Every checklist timing on one page: `amount__<key>__<variant>` and
    `unit__<key>__<variant>` per row, starting from the saved value or the code default."""

    def __init__(self, data=None, *, saved: dict | None = None):
        super().__init__(data)
        saved = schedule.load() if saved is None else saved
        for row in schedule.ROWS:
            if row.follows_message:
                continue
            amount, unit = saved.get((row.key, row.variant)) or schedule.code_default(
                row.key, row.variant
            )
            # A farm-in row shows only "Farmed-in orders", so its controls name both.
            name = row.label
            if row.variant == schedule.FARM:
                name = f"{row.label} (farmed-in orders)"
            self.fields[f"amount__{row.key}__{row.variant}"] = forms.IntegerField(
                min_value=0,
                initial=amount,
                label=f"{name} amount",
                widget=forms.NumberInput(
                    attrs={
                        "class": "field",
                        "min": 0,
                        "inputmode": "numeric",
                        "aria-label": f"{name} amount",
                    }
                ),
            )
            self.fields[f"unit__{row.key}__{row.variant}"] = forms.ChoiceField(
                choices=[(u, UNIT_LABELS[u]) for u in row.units],
                initial=unit,
                label=f"{name} unit",
            )

    def clean(self):
        cleaned = super().clean()
        for row in schedule.ROWS:
            if row.follows_message:
                continue
            amount_name = f"amount__{row.key}__{row.variant}"
            amount = cleaned.get(amount_name)
            unit = cleaned.get(f"unit__{row.key}__{row.variant}")
            if amount is not None and unit and amount > schedule.MAX_AMOUNT[unit]:
                self.add_error(
                    amount_name,
                    f"At most {schedule.MAX_AMOUNT[unit]:,} {UNIT_LABELS[unit]}.",
                )
        return cleaned

    def cleaned_values(self) -> dict[tuple[str, str], tuple[int, str]]:
        return {
            (row.key, row.variant): (
                self.cleaned_data[f"amount__{row.key}__{row.variant}"],
                self.cleaned_data[f"unit__{row.key}__{row.variant}"],
            )
            for row in schedule.ROWS
            if not row.follows_message
        }

    def groups(self) -> list[dict]:
        """The rows grouped for the template, each carrying its bound fields, its code
        default (for Reset) and a first example line computed from the current value."""
        out: list[dict] = []
        for row in schedule.ROWS:
            if row.follows_message:
                if not out or out[-1]["name"] != row.group:
                    out.append({"name": row.group, "items": []})
                out[-1]["items"].append(
                    {
                        "row": row,
                        "follows_message": True,
                        "example": schedule.driver_released_example(),
                    }
                )
                continue
            amount = self[f"amount__{row.key}__{row.variant}"]
            unit = self[f"unit__{row.key}__{row.variant}"]
            default_amount, default_unit = schedule.code_default(row.key, row.variant)
            try:
                example = schedule.preview(
                    row.key, row.variant, int(amount.value()), str(unit.value())
                )
            except (TypeError, ValueError):
                example = ""
            item = {
                "row": row,
                "amount": amount,
                "unit": unit,
                "unit_options": [(u, UNIT_LABELS[u]) for u in row.units],
                "single_unit": UNIT_LABELS[row.units[0]] if len(row.units) == 1 else "",
                "default_amount": default_amount,
                "default_unit": default_unit,
                "example": example,
            }
            if not out or out[-1]["name"] != row.group:
                out.append({"name": row.group, "items": []})
            out[-1]["items"].append(item)
        return out


class AffiliateDocumentTypeForm(forms.ModelForm):
    """ "Other" is where blank uploads are filed (``AffiliateDocumentType.objects.other()``
    finds it by name), so its name and active flag are locked; the rest stays editable."""

    class Meta:
        model = AffiliateDocumentType
        fields = ["name", "required", "sort_order", "active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field w-full"}),
            "sort_order": forms.NumberInput(attrs={"class": "field w-full", "min": 0}),
        }
        help_texts = {"sort_order": "Lower numbers appear first."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.protected = bool(self.instance.pk) and self.instance.name == "Other"
        if self.protected:
            for field in ("name", "active"):
                self.fields[field].disabled = True


class _EmailListInput(forms.MultipleHiddenInput):
    """Every posted value; never "omitted", so an empty list is saved as empty."""

    def value_omitted_from_data(self, data, files, name):
        return False


class EmailListField(forms.Field):
    """Staff emails: any number of posted values, each possibly holding several addresses
    split by commas or spaces; cleaned to a list without repeats."""

    widget = _EmailListInput

    def to_python(self, value) -> list[str]:
        chunks = [value] if isinstance(value, str) else list(value or [])
        out: list[str] = []
        for chunk in chunks:
            for part in re.split(r"[\s,;]+", chunk or ""):
                if part and part.lower() not in {e.lower() for e in out}:
                    out.append(part)
        return out

    def validate(self, value) -> None:
        super().validate(value)
        for email in value:
            validate_email(email)


AUDIENCE_ERROR = "This trigger can't send to that audience."
RECIPIENT_ERROR = "Pick people this audience can reach."
_TEXTS = ("subject", "email_body", "sms_body")
_ALL_RECIPIENTS = list(recipients.LABELS.items())


class MessageForm(forms.ModelForm):
    """One message in the editor (spec C section 6). Every choice is checked here, not only
    in the page: the audience must be one the trigger allows and each recipient one that
    audience can reach. `clean` runs the timing rules (`editor.check_timing`) and the
    save-time checks (`fields.validate`); `save` stores the row through `editor.save`."""

    TEXTS = _TEXTS

    trigger = forms.ChoiceField(
        choices=[(t.key, t.label) for t in triggers.CATALOG.values()],
        error_messages={
            "required": "Pick when this message goes out.",
            "invalid_choice": "Pick when this message goes out.",
        },
    )
    audience = forms.ChoiceField(
        choices=MessageTemplate.Audience.choices,
        error_messages={"invalid_choice": AUDIENCE_ERROR},
    )
    recipients = forms.MultipleChoiceField(
        required=False,
        choices=_ALL_RECIPIENTS,
        error_messages={"invalid_choice": RECIPIENT_ERROR},
    )
    staff_emails = EmailListField(required=False)
    timing_amount = forms.IntegerField(
        required=False, error_messages={"invalid": "Enter a whole number."}
    )
    timing_unit = forms.ChoiceField(required=False, choices=[(u, u) for u in editor.TIMING_UNITS])
    timing_direction = forms.ChoiceField(required=False, choices=MessageTemplate.Direction.choices)
    service_types = forms.ModelMultipleChoiceField(
        required=False, queryset=ServiceType.objects.none()
    )

    class Meta:
        model = MessageTemplate
        fields = [
            "name",
            "section",
            "audience",
            "trigger",
            "timing_amount",
            "timing_unit",
            "timing_direction",
            "send_sms",
            "send_email",
            "subject",
            "email_body",
            "sms_body",
            "recipients",
            "staff_emails",
            "service_types",
            "enabled",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "field", "aria-label": "Name"}),
            "subject": forms.TextInput(attrs={"class": "field", "aria-label": "Email subject"}),
            "email_body": forms.Textarea(attrs={"class": "field", "rows": 7}),
            "sms_body": forms.Textarea(attrs={"class": "field", "rows": 4}),
        }
        error_messages = {
            "name": {"required": "Give the message a name."},
            "subject": {"max_length": "Keep the subject under 200 characters."},
        }

    def __init__(self, data=None, *, instance: MessageTemplate):
        super().__init__(data, instance=instance)
        for name in _TEXTS:
            self.fields[name].strip = False
        trigger = triggers.CATALOG.get(self._current("trigger"))
        allowed = trigger.audiences if trigger else set(MessageTemplate.Audience.values)
        self.fields["audience"].choices = [
            (value, label) for value, label in MessageTemplate.Audience.choices if value in allowed
        ]
        self.fields["recipients"].choices = [
            (key, recipients.LABELS[key]) for key in recipients.keys_for(self._current("audience"))
        ]
        chosen = [st.pk for st in instance.service_types.all()] if instance.pk else []
        self.fields["service_types"].queryset = ServiceType.objects.filter(
            Q(active=True) | Q(pk__in=chosen)
        ).order_by("sort_order", "name")

    def _current(self, name: str) -> str:
        """A field as posted, else as stored: what the choices of the next field depend on."""
        if self.is_bound:
            return self.data.get(name) or ""
        return getattr(self.instance, name) or ""

    def clean(self):
        cleaned = super().clean()
        for name in _TEXTS:
            if name in cleaned:
                cleaned[name] = (cleaned[name] or "").replace("\r\n", "\n").replace("\r", "\n")
        trigger_key, audience = cleaned.get("trigger"), cleaned.get("audience")
        if not trigger_key:
            return cleaned
        if editor.locked_fields(self.instance)[0]:
            self._clean_locked(cleaned)
        elif not editor.can_pick(self.instance, trigger_key):
            self._error("trigger", editor.NOT_ADDABLE)
        if audience and audience not in triggers.CATALOG[trigger_key].audiences:
            self._error("audience", AUDIENCE_ERROR)
        staff = audience == MessageTemplate.Audience.STAFF
        picked = cleaned.get("recipients")
        if picked is not None:
            if staff:
                picked = []
            elif not cleaned.get("send_sms"):
                # Passengers and drivers are texted only: off an email-only message.
                picked = [key for key in picked if key not in recipients.TEXT_ONLY]
            cleaned["recipients"] = picked
        if audience and not staff and "staff_emails" in cleaned:
            cleaned["staff_emails"] = []
        self._clean_timing(cleaned, trigger_key)
        errors = fields.validate(
            {
                "trigger": trigger_key,
                "audience": audience or MessageTemplate.Audience.CUSTOMER,
                "send_sms": cleaned.get("send_sms"),
                "send_email": cleaned.get("send_email"),
                "subject": cleaned.get("subject", ""),
                "email_body": cleaned.get("email_body", ""),
                "sms_body": cleaned.get("sms_body", ""),
                "recipients": cleaned.get("recipients", self.instance.recipients),
                "staff_emails": cleaned.get("staff_emails", self.instance.staff_emails),
                "timing_direction": cleaned.get("timing_direction", ""),
                "key": self.instance.key,
                "seeded": self.instance.seeded,
            }
        )
        for name, message in errors.items():
            self._error("send_sms" if name == "channels" else name, message)
        return cleaned

    def _clean_locked(self, cleaned: dict) -> None:
        """A seeded staff-click row: its button sets who, how and when (`editor.LOCKED`); a
        seeded staff report: its job sets how and when (`editor.REPORT_LOCKED`). Both keep
        their service types. A change to any of them is refused, not saved."""
        locked, why = editor.locked_fields(self.instance)
        stored = MessageTemplate.objects.prefetch_related("service_types").get(pk=self.instance.pk)
        for name in locked:
            if name not in cleaned:
                if name in self.errors and self.data.get(name) != getattr(stored, name):
                    # A choice this row can't have (another audience): the reason is the lock.
                    self.errors.pop(name)
                    self._error(name, why)
                continue
            was, now = getattr(stored, name), cleaned[name]
            if isinstance(was, list):
                was, now = sorted(was), sorted(now or [])
            if was != now:
                self._error("send_sms" if name == "send_email" else name, why)
        if "service_types" in cleaned:
            was = {st.pk for st in stored.service_types.all()}
            if {st.pk for st in cleaned["service_types"]} != was:
                self._error("service_types", why)

    def _clean_timing(self, cleaned: dict, trigger_key: str) -> None:
        amount, unit = cleaned.get("timing_amount"), cleaned.get("timing_unit") or ""
        key, seeded = self.instance.key, self.instance.seeded
        if not editor.has_timing(trigger_key):
            timing = (amount, unit or "minutes") if amount else None
            cleaned["timing_amount"], cleaned["timing_unit"] = 0, "minutes"
        elif amount is None:
            self._error("timing_amount", "Enter a number.")
            return
        else:
            timing = (amount, unit)
        error = editor.check_timing(key, trigger_key, timing, seeded=seeded).get("timing")
        if error:
            self._error("timing_amount", error)
        cleaned["timing_direction"] = (
            cleaned.get("timing_direction") or MessageTemplate.Direction.AFTER
        )

    def _error(self, name: str, message: str) -> None:
        """One error per field: the first reason is the one worth showing."""
        if name not in self.errors:
            self.add_error(name, message)

    def save(self, user=None) -> editor.Redated:
        """Store the message; returns how many queued sends and checklist tasks were
        re-dated."""
        return editor.save(
            self.instance, user=user, service_types=self.cleaned_data.get("service_types", [])
        )
