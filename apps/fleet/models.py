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
        from django.db import connection

        # Lock the table to ensure consistency
        cls.objects.select_for_update().exists()

        # Query the auto_increment value, which persists across deletions
        db_name = connection.settings_dict["NAME"]
        table_name = cls._meta.db_table

        with connection.cursor() as cursor:
            if connection.vendor == "mysql":
                cursor.execute(
                    "SELECT AUTO_INCREMENT FROM INFORMATION_SCHEMA.TABLES"
                    " WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    [db_name, table_name],
                )
                result = cursor.fetchone()
                next_id = result[0] if result else 1
            else:
                # Fallback: use max(id) + 1 for non-MySQL databases
                next_id = (cls.objects.aggregate(models.Max("id"))["id__max"] or 0) + 1

        # driver_number = FIRST_NUMBER + (next_id - 1)
        # So if next_id is 1, driver_number is FIRST_NUMBER
        # If next_id is 2, driver_number is FIRST_NUMBER + 1, etc.
        return cls.FIRST_NUMBER + (next_id - 1)
