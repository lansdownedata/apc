"""Brakes on password guessing and on account mail, counted in the database.

No shared cache is configured (the default is `LocMemCache`, one per web worker), so a
count kept in the cache is really one count per worker and every limit multiplies by the
number of workers. These counts are rows instead: every worker reads the same one. The
per-account sign-in lock is separate and already on `CustomerAccount`.

A counter is a fixed window: it opens on the first hit and starts again once the window
has run out. Who is counted (an IP address, an email address) is stored only as a hash.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import Throttle


def _key(scope: str, who: str) -> str:
    return hashlib.sha256(f"{scope}:{who}".encode()).hexdigest()


def over(scope: str, who: str, limit: int) -> bool:
    """Whether `who` has reached `limit` hits in `scope`'s open window. Counts nothing."""
    return Throttle.objects.filter(
        key=_key(scope, who), expires_at__gt=timezone.now(), count__gte=limit
    ).exists()


def hit(scope: str, who: str, window: timedelta) -> int:
    """Count one more for `who` in `scope` and return the count so far, this one included.
    The row is locked while it is read and written, so two workers never lose a hit."""
    key, now = _key(scope, who), timezone.now()
    with transaction.atomic():
        row, created = Throttle.objects.select_for_update().get_or_create(
            key=key, defaults={"count": 1, "expires_at": now + window}
        )
        if not created:
            if row.expires_at <= now:
                row.count, row.expires_at = 1, now + window
            else:
                row.count += 1
            row.save(update_fields=["count", "expires_at"])
    if created:
        _sweep(key)
    return row.count


def _sweep(keep: str) -> None:
    """Delete the counters whose window has run out, so the table stays as small as the
    last window's traffic. Housekeeping only: it must never fail the request it rides on."""
    try:
        with transaction.atomic():
            Throttle.objects.filter(expires_at__lte=timezone.now()).exclude(key=keep).delete()
    except DatabaseError:
        pass
