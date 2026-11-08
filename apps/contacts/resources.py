"""The Contacts screen's Excel import / export (APC-141, spec §5)."""

from django.core.exceptions import ValidationError
from import_export.fields import Field
from import_export.instance_loaders import BaseInstanceLoader

from apps.core.choices import Channel
from apps.imports.resources import PortalResource, cell, row_no
from apps.imports.xlsx import Column

from .models import Contact

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


class ContactImportLoader(BaseInstanceLoader):
    def get_instance(self, row) -> Contact | None:
        return self.resource.match(row)


class ContactResource(PortalResource):
    COLUMNS = CONTACT_COLUMNS

    name = Field(attribute="name", column_name="Name")
    email = Field(attribute="email", column_name="Email")
    channel = Field(attribute="channel", column_name="Channel")
    notes = Field(attribute="notes", column_name="Notes")

    class Meta:
        model = Contact
        fields = ("name", "email", "channel", "notes")
        instance_loader_class = ContactImportLoader

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
        if new and not cell(row, "Name").strip():
            raise ValidationError("Name is required for a new contact.")
