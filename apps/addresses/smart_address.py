"""Server side of the smart-address widget's auto-save: write whichever fields the widget
posted onto an Address. Shared by the contact, vendor and driver endpoints."""

from django.http import QueryDict

from .models import Address

# Text fields the widget posts; place_id and the coordinates are handled separately below.
POSTED_FIELDS = (
    "landmark_name",
    "line1",
    "line2",
    "city",
    "state",
    "postal",
    "country",
    "place_type",
    "place_class",
    "display_name",
)


def apply_posted_address(address: Address, data: QueryDict) -> list[str]:
    """Write the posted fields onto `address` and save just those. Returns the changed
    field names (empty when nothing recognisable was posted)."""
    changed: list[str] = []
    for field in POSTED_FIELDS:
        if field in data:
            setattr(address, field, data.get(field, "").strip())
            changed.append(field)
    if "place_id" in data:
        address.locationiq_place_id = data.get("place_id", "").strip()
        changed.append("locationiq_place_id")
    for coord in ("latitude", "longitude"):
        if coord in data:
            raw = data.get(coord, "").strip()
            setattr(address, coord, raw or None)
            changed.append(coord)
    if changed:
        address.save(update_fields=[*changed, "updated_at"])
    return changed
