"""What a signed-in customer changes on their trips from the portal.

Every write resolves trips through `selectors.visible_trips(account)`, so an id from
another account is skipped, never written. `Reservation` is audited: each changed trip is
saved on its own with `update_fields`, so the trail records it under the request's source
(`customer portal`, `apps.audit.middleware`).
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction

from apps.reservations.models import Reservation

from .models import CustomerAccount
from .selectors import visible_trips

REFERENCE_MAX = Reservation._meta.get_field("customer_reference").max_length


def set_reference(account: CustomerAccount, trip_ids: Iterable[int], value: str) -> int:
    """Set "Your reference" on each of the account's trips in `trip_ids`, trimmed and cut
    to `REFERENCE_MAX` characters (blank clears it). Returns how many trips changed."""
    value = value.strip()[:REFERENCE_MAX].strip()
    trips = visible_trips(account).filter(pk__in=set(trip_ids)).prefetch_related(None)
    changed = 0
    with transaction.atomic():
        for trip in trips:
            if trip.customer_reference == value:
                continue
            trip.customer_reference = value
            trip.save(update_fields=["customer_reference", "updated_at"])
            changed += 1
    return changed
