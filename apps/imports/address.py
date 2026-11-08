"""The five address cells, shared by the Contacts and Vendors imports (APC-141, spec §5.4).

Saved as typed, never geocoded. Model-agnostic: the owner is any model with a nullable
`Address` foreign key, named by `slot` ("primary_address" on a contact, "address" on a vendor).
"""

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator

from apps.addresses.models import Address

ADDRESS_COLUMNS = {
    "Street": "line1",
    "Street 2": "line2",
    "City": "city",
    "State": "state",
    "ZIP": "postal",
}

# Cleared when a text field changes, so the old pin never sits under the new street.
LOCATION_FIELDS = ("latitude", "longitude", "locationiq_place_id", "display_name")


def _typed(row) -> dict[str, str]:
    """The non-blank address cells of `row`, by Address field."""
    typed = {}
    for column, attr in ADDRESS_COLUMNS.items():
        value = (row.get(column) or "").strip()
        if value:
            typed[attr] = value
    return typed


def clean_address(row) -> None:
    """Raise the row's `ValidationError` for a cell longer than its field. Writes nothing."""
    columns = {attr: column for column, attr in ADDRESS_COLUMNS.items()}
    for attr, value in _typed(row).items():
        limit = Address._meta.get_field(attr).max_length
        try:
            MaxLengthValidator(limit)(value)
        except ValidationError as error:
            raise ValidationError(f"{columns[attr]}: {error.messages[0]}") from error


def address_changes(address: Address | None, row) -> list[list[str]]:
    """`[["Street", old, new], ...]` for the non-blank cells that differ from what is stored."""
    changes = []
    typed = _typed(row)
    for column, attr in ADDRESS_COLUMNS.items():
        if attr not in typed:
            continue
        old = getattr(address, attr) if address is not None else ""
        if typed[attr] != old:
            changes.append([column, old, typed[attr]])
    return changes


def address_cell(address: Address | None, column: str) -> str:
    """One address cell for the export."""
    return getattr(address, ADDRESS_COLUMNS[column]) if address is not None else ""


def write_address(owner, slot: str, row) -> None:
    """Write the non-blank cells onto `owner`'s address, creating it when there is none.

    The caller saves `owner` (a new Address is only set on it). A changed text field clears
    the location data. Country is left as it is.
    """
    typed = _typed(row)
    if not typed:
        return
    address = getattr(owner, slot)
    if address is None:
        address = Address(**typed)
        address.save()
        setattr(owner, slot, address)
        return
    changed = [attr for attr, value in typed.items() if getattr(address, attr) != value]
    if not changed:
        return
    for attr in changed:
        setattr(address, attr, typed[attr])
    if address.latitude is not None or address.longitude is not None:
        address.latitude = address.longitude = None
        address.locationiq_place_id = address.display_name = ""
        changed += LOCATION_FIELDS
    address.save(update_fields=[*changed, "updated_at"])
