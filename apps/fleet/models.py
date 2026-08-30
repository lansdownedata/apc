"""In-house drivers, fleet units, and the renewals (licences, registrations, permits) that
keep them legal to run. Affiliate rosters stay in apps.vendors — this app is APC's own."""

from __future__ import annotations

from django.db import IntegrityError, models, transaction

from apps.core.models import TimeStampedModel
from apps.core.phone import to_e164


class Driver(TimeStampedModel):
    """An in-house driver. `driver_number` is allocated by `save()` and never edited."""

    FIRST_NUMBER = 1000
    ALLOCATION_ATTEMPTS = 5

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    driver_number = models.PositiveIntegerField(unique=True, editable=False)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    home_address = models.ForeignKey(
        "addresses.Address", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.driver_number} · {self.name}"

    def save(self, *args, **kwargs):
        """Allocate the driver number on first save.

        This is the first counter pattern in the codebase (quote numbers are derived from
        the PK at display time). "Lock the top row, insert max+1" alone is not safe: a
        transaction that blocked on the old top row returns that same row once the lock
        clears even if the winner inserted a higher one meanwhile, and an empty table has
        no row to lock at all. So the unique constraint is the real guard — an
        IntegrityError rolls back just the savepoint and the allocation is retried against
        the fresh top. Numbers are never reused: a deleted driver leaves a gap.
        """
        self.phone = to_e164(self.phone) or (self.phone or "").strip()
        if self.driver_number is not None:
            return super().save(*args, **kwargs)
        for _attempt in range(self.ALLOCATION_ATTEMPTS):
            try:
                with transaction.atomic():
                    self.driver_number = self._next_number()
                    return super().save(*args, **kwargs)
            except IntegrityError:
                continue
        raise IntegrityError("Could not allocate a driver number — too many concurrent creates.")

    @classmethod
    def _next_number(cls) -> int:
        """max + 1, read under a row lock so a retry always sees the latest committed top."""
        top = (
            cls.objects.select_for_update()
            .order_by("-driver_number")
            .values_list("driver_number", flat=True)
            .first()
        )
        return cls.FIRST_NUMBER if top is None else top + 1
