"""The Contacts screen's Excel import / export (APC-141, spec §5)."""

import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from import_export.fields import Field
from import_export.instance_loaders import BaseInstanceLoader

from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.core.templatetags.phone_filters import phone_display
from apps.imports.address import (
    ADDRESS_COLUMNS,
    address_cell,
    address_changes,
    clean_address,
    write_address,
)
from apps.imports.resources import PortalResource, cell, row_no
from apps.imports.xlsx import Column

from . import services
from .models import Company, Contact, ContactPhone

CONTACT_COLUMNS = (
    Column("Name", required=True),
    Column("Email"),
    Column("Phone", text=True),
    Column("Other phones", text=True),
    Column("Company"),
    Column("Channel", choices="A"),
    Column("Street"),
    Column("Street 2"),
    Column("City"),
    Column("State"),
    Column("ZIP", text=True),
    Column("Notes"),
)

CHANNELS = {
    **{value.lower(): value for value in Channel.values},
    **{label.lower(): value for value, label in Channel.choices},
}

PHONE_LABELS = {
    **{value.lower(): value for value in ContactPhone.Label.values},
    **{label.lower(): value for value, label in ContactPhone.Label.choices},
}
# "(202) 555-0199 (Work)": the last parenthesised group is the label.
LABELLED_PHONE = re.compile(r"^(?P<number>.*?)\s*\((?P<label>[^()]*)\)\s*$")


def parse_other_phones(text: str) -> list[tuple[str, str, str | None, str]]:
    """`(item, number as typed, E.164 or None, label value or "")` per comma/semicolon item.

    An unknown label is the row's error; whether a number can be dialled is decided once the
    contact is known (a number already on file is never re-validated).
    """
    items = []
    for item in re.split(r"[,;]", text):
        item = item.strip()
        if not item:
            continue
        labelled = LABELLED_PHONE.match(item)
        number, label = (labelled["number"], labelled["label"].strip()) if labelled else (item, "")
        value = ""
        if label:
            value = PHONE_LABELS.get(label.lower())
            if value is None:
                raise ValidationError(
                    f"Phone label '{label}' isn't one of: {', '.join(ContactPhone.Label.labels)}"
                )
        items.append((item, number.strip(), to_e164(number), value))
    return items


def same_number(stored: str, typed: str, e164: str | None) -> bool:
    """`typed` is `stored` as typed, as E.164 or as `phone_display` shows it."""
    return bool(stored) and (typed == stored or e164 == stored or typed == phone_display(stored))


def phone_with_label(number: str, label: str) -> str:
    """`(202) 555-0199 (Work)`, or the bare number when there is no label."""
    shown = phone_display(number)
    return f"{shown} ({ContactPhone.Label(label).label})" if label else shown


def _address_field(column: str) -> Field:
    return Field(
        column_name=column,
        dehydrate_method=lambda contact: address_cell(contact.primary_address, column),
    )


class ContactImportLoader(BaseInstanceLoader):
    def get_instance(self, row) -> Contact | None:
        return self.resource.match(row)


class ContactResource(PortalResource):
    COLUMNS = CONTACT_COLUMNS

    name = Field(attribute="name", column_name="Name")
    email = Field(attribute="email", column_name="Email")
    phone = Field(column_name="Phone", dehydrate_method="dehydrate_phone")
    other_phones = Field(column_name="Other phones", dehydrate_method="dehydrate_other_phones")
    company = Field(column_name="Company", dehydrate_method="dehydrate_company")
    channel = Field(
        attribute="channel", column_name="Channel", dehydrate_method="dehydrate_channel"
    )
    street = _address_field("Street")
    street_2 = _address_field("Street 2")
    city = _address_field("City")
    state = _address_field("State")
    zip = _address_field("ZIP")
    notes = Field(attribute="notes", column_name="Notes")

    class Meta:
        model = Contact
        fields = (
            "name",
            "email",
            "phone",
            "other_phones",
            "company",
            "channel",
            "street",
            "street_2",
            "city",
            "state",
            "zip",
            "notes",
        )
        instance_loader_class = ContactImportLoader

    @classmethod
    def export_queryset(cls, queryset):
        return queryset.select_related("company", "primary_address").prefetch_related("phones")

    # --- export --------------------------------------------------------------------------

    def dehydrate_phone(self, contact: Contact) -> str:
        return phone_display(contact.phone)

    def dehydrate_other_phones(self, contact: Contact) -> str:
        """The numbers that aren't the texting one, oldest first. Reads `phones.all()` so the
        export's prefetch serves it."""
        others = sorted(
            (row for row in contact.phones.all() if not row.texting),
            key=lambda row: (row.created_at, row.pk),
        )
        return ", ".join(phone_with_label(row.number, row.label) for row in others)

    def dehydrate_company(self, contact: Contact) -> str:
        return contact.company.name if contact.company_id else ""

    def dehydrate_channel(self, contact: Contact) -> str:
        return contact.get_channel_display()

    # --- import: parse and validate (nothing written) ------------------------------------

    def before_import_row(self, row, **kwargs) -> None:
        super().before_import_row(row, **kwargs)
        if "Email" in row:
            row["Email"] = cell(row, "Email").strip().lower()
        typed = cell(row, "Channel").strip()
        if typed:
            if typed.lower() not in CHANNELS:
                raise ValidationError(
                    f"Channel '{typed}' isn't one of: {', '.join(Channel.labels)}"
                )
            row["Channel"] = CHANNELS[typed.lower()]
        phone = cell(row, "Phone").strip()
        company = cell(row, "Company").strip()
        if company:
            limit = Company._meta.get_field("name").max_length
            try:
                MaxLengthValidator(limit)(company)
            except ValidationError as error:
                raise ValidationError(f"Company: {error.messages[0]}") from error
        clean_address(row)
        self._parsed.update(
            phone=phone,
            phone_e164=to_e164(phone),
            others=parse_other_phones(cell(row, "Other phones")),
            company_typed=company,
        )

    def match(self, row) -> Contact | None:
        """Spec §5.2: email decides; a phone fills an email in only on an email-less contact."""
        note = self.notes[row_no(row)]
        email, phone = cell(row, "Email").strip(), cell(row, "Phone").strip()
        if email:
            found = Contact.objects.find_match(email=email)
            if found is not None or not phone:
                note["key"] = email
                return found
            found = Contact.objects.find_by_phone(phone, without_email=True)
            if found is not None:
                note["key"] = phone
                return found
            note["key"] = email
            other = Contact.objects.find_by_phone(phone)
            if other is not None:
                note["warnings"].append(
                    f"Phone is also on {other.name} ({other.email}) — this creates a separate "
                    "contact"
                )
            return None
        if phone:
            note["key"] = phone
            return Contact.objects.find_match(phone=phone)
        raise ValidationError("Needs an email or a phone to match or create a contact.")

    def after_init_instance(self, instance, new, row, **kwargs) -> None:
        """Decide the phones and the company against the matched contact. Reads only."""
        super().after_init_instance(instance, new, row, **kwargs)
        if new and not cell(row, "Name").strip():
            raise ValidationError("Name is required for a new contact.")
        parsed = self._parsed
        on_file = (
            [] if new or not (parsed["phone"] or parsed["others"]) else list(instance.phones.all())
        )
        self._resolve_phone(instance, new, on_file)
        self._resolve_other_phones(on_file)
        self._resolve_company(instance, new)

    def _resolve_phone(self, instance: Contact, new: bool, on_file: list[ContactPhone]) -> None:
        """Spec §5.3: a new contact's Phone is its texting number and must dial; on an update
        a number already on file is left alone, and a different one is added."""
        parsed = self._parsed
        parsed["phone_set"] = parsed["phone_add"] = None
        typed, e164 = parsed["phone"], parsed["phone_e164"]
        if not typed:
            return
        stored = [instance.phone, *(row.number for row in on_file)]
        if not new and any(same_number(number, typed, e164) for number in stored):
            return
        if e164 is None:
            raise ValidationError(f"Phone '{typed}' isn't a phone number we can dial.")
        parsed["phone_set" if new else "phone_add"] = e164

    def _resolve_other_phones(self, on_file: list[ContactPhone]) -> None:
        """Add-only: numbers not on file are added; a given label that differs is updated."""
        parsed = self._parsed
        parsed["adds"], parsed["relabels"] = [], []
        for item, number, e164, label in parsed["others"]:
            held = next((row for row in on_file if same_number(row.number, number, e164)), None)
            if held is not None:
                if label and held.label != label:
                    parsed["relabels"].append((held, label))
            elif e164 is None:
                raise ValidationError(f"Other phones: '{item}' isn't a phone number we can dial.")
            else:
                parsed["adds"].append((e164, label))

    def _resolve_company(self, instance: Contact, new: bool) -> None:
        """By name, case-insensitive. A name not on file is created on save, never here."""
        parsed = self._parsed
        parsed["company_changed"] = False
        typed = parsed["company_typed"]
        if not typed:
            return
        current = instance.company if instance.company_id else None
        if current is not None and current.name.lower() == typed.lower():
            return
        found = Company.objects.filter(name__iexact=typed).first()
        if current is not None and found is not None and found.pk == current.pk:
            return
        parsed.update(
            company_changed=True,
            company=found,
            company_name=found.name if found is not None else typed,
            company_old=current.name if current is not None else "",
        )

    def display_virtual(self, column: str, value: str) -> str:
        parsed = self._parsed
        if column == "Phone":
            return phone_display(parsed["phone_set"])
        if column == "Other phones":
            return ", ".join(phone_with_label(number, label) for number, label in parsed["adds"])
        if column == "Company":
            return parsed["company_name"]
        return super().display_virtual(column, value)

    def virtual_changes(self, instance: Contact, row) -> list[list[str]]:
        parsed = self._parsed
        changes = []
        if parsed["phone_add"]:
            shown = phone_display(parsed["phone_add"])
            # add_phone makes it the texting number only when the contact has none.
            changes.append(
                ["Other phones", "", f"+ {shown}"] if instance.phone else ["Phone", "", shown]
            )
        for number, label in parsed["adds"]:
            changes.append(["Other phones", "", f"+ {phone_with_label(number, label)}"])
        for held, label in parsed["relabels"]:
            changes.append(
                [
                    "Other phones",
                    phone_with_label(held.number, held.label),
                    phone_with_label(held.number, label),
                ]
            )
        if parsed["company_changed"]:
            changes.append(["Company", parsed["company_old"], parsed["company_name"]])
        if any(cell(row, column).strip() for column in ADDRESS_COLUMNS):
            changes += address_changes(instance.primary_address, row)
        return changes

    # --- import: write (only once the row is fully validated) ----------------------------

    def before_save_instance(self, instance: Contact, row, **kwargs) -> None:
        parsed = self._parsed
        if parsed["phone_set"]:
            instance.phone = parsed["phone_set"]
        if parsed["company_changed"]:
            instance.company = parsed["company"] or Company.objects.get_or_create_by_name(
                parsed["company_name"]
            )
        write_address(instance, "primary_address", row)

    def after_save_instance(self, instance: Contact, row, **kwargs) -> None:
        """Phones go through `services.add_phone` (a dry run's writes roll back with it); the
        farm-in refresh is a side effect, so only for real (spec §5.5)."""
        super().after_save_instance(instance, row, **kwargs)
        parsed = self._parsed
        if parsed["phone_add"]:
            services.add_phone(instance, parsed["phone_add"])
        for held, label in parsed["relabels"]:
            held.label = label
            held.save(update_fields=["label", "updated_at"])
        for number, label in parsed["adds"]:
            services.add_phone(instance, number, label=label)
        if parsed["company_changed"] and not parsed["new"] and not kwargs.get("dry_run"):
            from apps.tasks import services as task_services  # local: tasks imports leads

            task_services.refresh_farm_in_for_contact(instance)
