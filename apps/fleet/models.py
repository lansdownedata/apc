"""In-house drivers, fleet units, and the renewals (licences, registrations, permits) that
keep them legal to run. Affiliate rosters stay in apps.vendors — this app is APC's own."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, models, transaction
from django.db.models import Prefetch, Q
from django.db.models.functions import Lower
from django.utils import timezone

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

    @property
    def current_renewals(self) -> list[Renewal]:
        return current_renewals(self.renewals.all())

    def renewal_summary(self) -> dict:
        return renewal_summary(self.renewals.all())

    @property
    def renewal_status(self) -> str:
        return self.renewal_summary()["status"]

    @property
    def needs_attention(self) -> bool:
        return self.renewal_status in RENEWAL_ATTENTION

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


# Worst-first severity for roll-ups and the attention split. Mirrors
# vendors.INSURANCE_SEVERITY — two concrete uses, not three, so mirrored rather than shared.
RENEWAL_SEVERITY = ("expired", "critical", "urgent", "expiring", "valid")
# Unlike vendor insurance, "nothing on file" is not an attention state for fleet paperwork.
RENEWAL_ATTENTION = frozenset({"expired", "critical", "urgent", "expiring"})


class Vehicle(TimeStampedModel):
    """A fleet unit — one physical vehicle — distinct from the rate-card class it belongs to."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=120)
    vehicle_type = models.ForeignKey(
        "leads.VehicleType", on_delete=models.PROTECT, related_name="units"
    )
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    make = models.CharField(max_length=60, blank=True)
    # Not `model`: a field literally named model invites confusion with Meta.model.
    model_name = models.CharField("Model", max_length=60, blank=True)
    color = models.CharField(max_length=40, blank=True)
    license_plate = models.CharField(max_length=20, blank=True)
    vin = models.CharField("VIN", max_length=17, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def current_renewals(self) -> list[Renewal]:
        return current_renewals(self.renewals.all())

    def renewal_summary(self) -> dict:
        return renewal_summary(self.renewals.all())

    @property
    def renewal_status(self) -> str:
        return self.renewal_summary()["status"]

    @property
    def needs_attention(self) -> bool:
        return self.renewal_status in RENEWAL_ATTENTION


class RenewalType(TimeStampedModel):
    """The shared catalog: what kinds of paperwork a driver or a vehicle carries."""

    class AppliesTo(models.TextChoices):
        DRIVER = "driver", "Driver"
        VEHICLE = "vehicle", "Vehicle"

    name = models.CharField(max_length=120)
    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "applies_to",
                name="uniq_renewal_type_name_ci",
                violation_error_message=(
                    "A renewal type with that name already exists for that subject."
                ),
            )
        ]

    def __str__(self) -> str:
        return self.name


class Renewal(TimeStampedModel):
    """One instance of a renewal type for one subject. Append-only: renewing creates a
    new row and the old one becomes history. Dates are plain calendar dates compared
    against timezone.localdate() — no trip timezone is involved.

    Urgency is the same graded ramp as VendorInsurance, so the chips read the same:
    valid > expiring(30) > urgent(15) > critical(10, incl. the expiry day) > expired.
    """

    EXPIRING_DAYS = 30
    URGENT_DAYS = 15
    CRITICAL_DAYS = 10

    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        EXPIRING = "expiring", "Expiring"
        URGENT = "urgent", "Urgent"
        CRITICAL = "critical", "Critical"
        EXPIRED = "expired", "Expired"

    renewal_type = models.ForeignKey(RenewalType, on_delete=models.PROTECT, related_name="renewals")
    driver = models.ForeignKey(
        Driver, null=True, blank=True, on_delete=models.CASCADE, related_name="renewals"
    )
    vehicle = models.ForeignKey(
        Vehicle, null=True, blank=True, on_delete=models.CASCADE, related_name="renewals"
    )
    reference = models.CharField(max_length=80, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField()
    document = models.FileField(upload_to="renewals/", blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-expires_on"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(driver__isnull=False, vehicle__isnull=True)
                    | Q(driver__isnull=True, vehicle__isnull=False)
                ),
                name="renewal_one_subject",
            )
        ]

    def __str__(self) -> str:
        return f"{self.renewal_type.name} · exp {self.expires_on:%Y-%m-%d}"

    @property
    def subject(self) -> Driver | Vehicle:
        return self.driver or self.vehicle

    @property
    def days_until_expiry(self) -> int:
        return (self.expires_on - timezone.localdate()).days

    @property
    def status(self) -> str:
        days = self.days_until_expiry
        if days < 0:
            return self.Status.EXPIRED
        if days <= self.CRITICAL_DAYS:
            return self.Status.CRITICAL
        if days <= self.URGENT_DAYS:
            return self.Status.URGENT
        if days <= self.EXPIRING_DAYS:
            return self.Status.EXPIRING
        return self.Status.VALID

    @property
    def label(self) -> str:
        days = self.days_until_expiry
        if self.status == self.Status.EXPIRED:
            n = abs(days)
            return f"Lapsed {n} day{'s' if n != 1 else ''} ago"
        if self.status == self.Status.VALID:
            return f"Valid · exp {self.expires_on:%b '%y}"
        return "Expires today" if days == 0 else f"Expires in {days} day{'s' if days != 1 else ''}"


# Every reader of a subject's renewals goes through this prefetch: the roll-ups touch
# `renewal_type` on each row, so the join has to ride along or every row costs a query.
RENEWAL_PREFETCH = Prefetch("renewals", queryset=Renewal.objects.select_related("renewal_type"))


def current_renewals(rows: Iterable[Renewal]) -> list[Renewal]:
    """The record that counts per type — the one expiring last. Older rows are history."""
    best: dict[int, Renewal] = {}
    for row in rows:
        held = best.get(row.renewal_type_id)
        if held is None or row.expires_on > held.expires_on:
            best[row.renewal_type_id] = row
    return sorted(
        best.values(), key=lambda r: (r.renewal_type.sort_order, r.renewal_type.name.lower())
    )


def renewal_summary(rows: Iterable[Renewal]) -> dict:
    """Status + label of the governing (worst, then soonest) current record — drives the
    list chips, the detail banner and the dispatch-drawer warning."""
    current = current_renewals(rows)
    if not current:
        return {
            "status": "none",
            "days": None,
            "expiry": None,
            "label": "Nothing on file",
            "type": "",
        }
    rank = {s: i for i, s in enumerate(RENEWAL_SEVERITY)}
    worst = min(current, key=lambda r: (rank[r.status], r.expires_on))
    return {
        "status": worst.status,
        "days": worst.days_until_expiry,
        "expiry": worst.expires_on,
        "label": worst.label,
        "type": worst.renewal_type.name,
    }
