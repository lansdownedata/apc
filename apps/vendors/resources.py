"""The Vendors screen's Excel import / export (APC-141, spec §6)."""

from django.core.exceptions import ValidationError
from django.db.models import Q
from import_export.fields import Field
from import_export.instance_loaders import BaseInstanceLoader

from apps.billing.models import SyncState
from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display
from apps.imports.address import (
    ADDRESS_COLUMNS,
    address_cell,
    address_changes,
    clean_address,
    write_address,
)
from apps.imports.resources import (
    PortalResource,
    cell,
    export_phone,
    phone_match_warning,
    row_no,
    same_number,
)
from apps.imports.xlsx import Column
from apps.leads.models import VehicleType
from apps.tasks import vendor_tasks

from .models import Vendor

VENDOR_COLUMNS = (
    Column("Name", required=True),
    Column("Contact name"),
    Column("Email"),
    Column("Phone", text=True),
    Column("Status", choices="A"),
    Column("Vehicle types"),
    Column("Service area"),
    Column("Website"),
    Column("USDOT number", text=True),
    Column("Street"),
    Column("Street 2"),
    Column("City"),
    Column("State"),
    Column("ZIP", text=True),
    Column("Notes"),
)

# "Archived" is the vendor list's word for an inactive vendor (apps/vendors/views.py).
STATUSES = {
    **{value: value for value in Vendor.Status.values},
    **{label.lower(): value for value, label in Vendor.Status.choices},
    "archived": Vendor.Status.INACTIVE,
}


def type_names(types) -> str:
    """`Sedan, SUV`: names sorted case-insensitively, as the preview and the export show them."""
    return ", ".join(sorted((t.name for t in types), key=str.lower))


def _phone_lookup(typed: str, e164: str | None) -> Q:
    """As `VendorManager.find_match` normalises: the number as typed, or its E.164."""
    lookup = Q(phone__iexact=typed)
    if e164 and e164 != typed:
        lookup |= Q(phone__iexact=e164)
    return lookup


def _only(queryset, key: str) -> Vendor | None:
    """The one vendor `queryset` holds, None for none; more than one is the row's error."""
    vendors = list(queryset.order_by("pk"))
    if len(vendors) > 1:
        named = ", ".join(f"{v.name} (#{v.pk})" for v in vendors)
        raise ValidationError(
            f"{key} matches {len(vendors)} vendors: {named}. Fix the duplicates first."
        )
    return vendors[0] if vendors else None


def _address_field(column: str) -> Field:
    return Field(
        column_name=column,
        dehydrate_method=lambda vendor: address_cell(vendor.address, column),
    )


class VendorImportLoader(BaseInstanceLoader):
    def get_instance(self, row) -> Vendor | None:
        return self.resource.match(row)


class VendorResource(PortalResource):
    COLUMNS = VENDOR_COLUMNS
    ADDRESS_SLOT = "address"

    name = Field(attribute="name", column_name="Name")
    contact_name = Field(attribute="contact_name", column_name="Contact name")
    email = Field(attribute="email", column_name="Email")
    phone = Field(attribute="phone", column_name="Phone", dehydrate_method="dehydrate_phone")
    status = Field(attribute="status", column_name="Status", dehydrate_method="dehydrate_status")
    vehicle_types = Field(column_name="Vehicle types", dehydrate_method="dehydrate_vehicle_types")
    service_area = Field(attribute="service_area", column_name="Service area")
    website = Field(attribute="website", column_name="Website")
    usdot_number = Field(attribute="usdot_number", column_name="USDOT number")
    street = _address_field("Street")
    street_2 = _address_field("Street 2")
    city = _address_field("City")
    state = _address_field("State")
    zip = _address_field("ZIP")
    notes = Field(attribute="notes", column_name="Notes")

    class Meta:
        model = Vendor
        fields = (
            "name",
            "contact_name",
            "email",
            "phone",
            "status",
            "vehicle_types",
            "service_area",
            "website",
            "usdot_number",
            "street",
            "street_2",
            "city",
            "state",
            "zip",
            "notes",
        )
        instance_loader_class = VendorImportLoader

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._types: dict[str, VehicleType] = {}

    @classmethod
    def export_queryset(cls, queryset):
        return queryset.select_related("address").prefetch_related("vehicle_types")

    # --- export --------------------------------------------------------------------------

    def dehydrate_phone(self, vendor: Vendor) -> str:
        return export_phone(vendor.phone)

    def dehydrate_status(self, vendor: Vendor) -> str:
        return vendor.get_status_display()

    def dehydrate_vehicle_types(self, vendor: Vendor) -> str:
        """Reads `vehicle_types.all()` so the export's prefetch serves it."""
        return type_names(vendor.vehicle_types.all())

    # --- import: parse and validate (nothing written) ------------------------------------

    def before_import(self, dataset, **kwargs) -> None:
        """Every vehicle type, active or not, once per run (spec §6.3)."""
        super().before_import(dataset, **kwargs)
        self._types = {t.name.lower(): t for t in VehicleType.objects.all()}

    def before_import_row(self, row, **kwargs) -> None:
        super().before_import_row(row, **kwargs)
        if "Email" in row:
            row["Email"] = cell(row, "Email").strip()
        phone = cell(row, "Phone").strip()
        e164 = to_e164(phone)
        if phone:
            row["Phone"] = e164 or phone
        typed = cell(row, "Status").strip()
        if typed:
            if typed.lower() not in STATUSES:
                raise ValidationError("Status must be Active or Inactive")
            row["Status"] = STATUSES[typed.lower()]
        website = cell(row, "Website").strip()
        if website and "://" not in website:
            row["Website"] = f"https://{website}"  # as VendorForm's URLField takes it
        clean_address(row)
        self._parsed.update(phone=phone, phone_e164=e164, types=self._parse_types(row))

    def _parse_types(self, row) -> list[VehicleType] | None:
        """The named types, or None for a blank cell (unchanged). Never creates a type."""
        names = [n.strip() for n in cell(row, "Vehicle types").split(",") if n.strip()]
        if not names:
            return None
        unknown = list({n.lower(): n for n in names if n.lower() not in self._types}.values())
        if unknown:
            raise ValidationError(
                "; ".join(f"Vehicle type '{name}' doesn't exist" for name in unknown)
            )
        return list({self._types[n.lower()].pk: self._types[n.lower()] for n in names}.values())

    def match(self, row) -> Vendor | None:
        """Spec §6.2, the import's own rule (not `VendorManager.find_match`): email decides; a
        phone fills an email in only on an email-less vendor; more than one match is an error."""
        note = self.notes[row_no(row)]
        email, phone = cell(row, "Email").strip(), self._parsed["phone"]
        by_phone = (
            Vendor.objects.filter(_phone_lookup(phone, self._parsed["phone_e164"]))
            if phone
            else None
        )
        if email:
            found = _only(Vendor.objects.filter(email__iexact=email), "Email")
            if found is not None or not phone:
                note["key"] = email
                return found
            found = _only(by_phone.filter(email=""), "Phone")
            if found is not None:
                note["key"] = phone
                return found
            note["key"] = email
            other = by_phone.exclude(email="").order_by("pk").first()
            if other is not None:
                note["warnings"].append(
                    f"Phone is also on {other.name} ({other.email}) — this creates a separate "
                    "vendor"
                )
            return None
        if phone:
            note["key"] = phone
            found = _only(by_phone, "Phone")
            if found is not None and found.email:
                note["warnings"].append(phone_match_warning(found))
            return found
        raise ValidationError("Needs an email or a phone to match or create a vendor.")

    def after_init_instance(self, instance, new, row, **kwargs) -> None:
        """Settle what counts as unchanged against the matched vendor. Reads only."""
        super().after_init_instance(instance, new, row, **kwargs)
        if new and not cell(row, "Name").strip():
            raise ValidationError("Name is required for a new vendor.")
        parsed = self._parsed
        watched = ("status", *Vendor.QBO_FIELDS)
        parsed["before"] = {attr: getattr(instance, attr) for attr in watched}
        if not new:
            email = cell(row, "Email")
            if email and email.lower() == instance.email.lower():
                row["Email"] = instance.email  # matched case-insensitively: not a change
            if parsed["phone"] and same_number(
                instance.phone, parsed["phone"], parsed["phone_e164"]
            ):
                row["Phone"] = instance.phone  # the number on file, however it was stored
        types = parsed["types"]
        parsed["types_old"] = [] if new or types is None else list(instance.vehicle_types.all())
        parsed["types_changed"] = types is not None and (
            {t.pk for t in types} != {t.pk for t in parsed["types_old"]}
        )

    def _display(self, instance, field) -> str:
        if field.attribute == "phone":
            return phone_display(field.get_value(instance))
        return super()._display(instance, field)

    def display_virtual(self, column: str, value: str) -> str:
        if column == "Vehicle types":
            return type_names(self._parsed["types"])
        return super().display_virtual(column, value)

    def virtual_changes(self, instance: Vendor, row) -> list[list[str]]:
        parsed = self._parsed
        changes = []
        if parsed["types_changed"]:
            changes.append(
                ["Vehicle types", type_names(parsed["types_old"]), type_names(parsed["types"])]
            )
        if any(cell(row, column).strip() for column in ADDRESS_COLUMNS):
            changes += address_changes(instance.address, row)
        return changes

    # --- import: write (only once the row is fully validated) ----------------------------

    def before_save_instance(self, instance: Vendor, row, **kwargs) -> None:
        """Its QuickBooks vendor needs a re-push when name, email or phone change (§6.4)."""
        before = self._parsed["before"]
        if any(getattr(instance, attr) != before[attr] for attr in Vendor.QBO_FIELDS):
            instance.qbo_sync_state = SyncState.NOT_SYNCED
        write_address(instance, "address", row)

    def save_m2m(self, instance: Vendor, row, **kwargs) -> None:
        """A non-blank Vehicle types cell replaces the set (§6.3)."""
        super().save_m2m(instance, row, **kwargs)
        if self._parsed["types_changed"]:
            instance.vehicle_types.set(self._parsed["types"])

    def after_save_instance(self, instance: Vendor, row, **kwargs) -> None:
        """A new vendor or a status change runs the vendor's task pass, as `vendor_edit` does
        (deactivating retires its renewal tasks); a side effect, so only for real (§6.4)."""
        super().after_save_instance(instance, row, **kwargs)
        parsed = self._parsed
        status_changed = instance.status != parsed["before"]["status"]
        if not kwargs.get("dry_run") and (parsed["new"] or status_changed):
            vendor_tasks.sync_vendor(instance)
