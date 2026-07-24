from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import MoneyField
from apps.core.models import TimeStampedModel
from apps.core.phone import to_e164


class VendorManager(models.Manager):
    """Dedupe helpers — mirrors apps.contacts.ContactManager so we don't create
    duplicate vendors when the same affiliate is entered twice."""

    def find_match(self, *, phone: str = "", email: str = "") -> "Vendor | None":
        phone, email = (phone or "").strip(), (email or "").strip()
        lookup = Q()
        if phone:
            lookup |= Q(phone__iexact=phone)
            normalized = to_e164(phone)
            if normalized and normalized != phone:
                lookup |= Q(phone__iexact=normalized)
        if email:
            lookup |= Q(email__iexact=email)
        if not lookup:
            return None
        return self.filter(lookup).order_by("-created_at").first()

    def match_or_create(
        self,
        *,
        name: str,
        contact_name: str = "",
        phone: str = "",
        email: str = "",
    ) -> "Vendor":
        existing = self.find_match(phone=phone, email=email)
        if existing is not None:
            return existing
        return self.create(
            name=name,
            contact_name=contact_name,
            phone=to_e164(phone) or (phone or "").strip(),
            email=(email or "").strip(),
        )


# Worst-first severity for insurance rollups + the directory's attention strip.
INSURANCE_SEVERITY = ("expired", "critical", "urgent", "expiring", "valid")
INSURANCE_ATTENTION = frozenset({"expired", "critical", "urgent", "expiring", "none"})


class Vendor(TimeStampedModel):
    """A farm-out affiliate company. Standalone — no LimoAnywhere link (see spec)."""

    objects = VendorManager()

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    service_area = models.CharField(max_length=200, blank=True)
    # Mailing address is a shared addresses.Address (LocationIQ smart-address), set on the
    # vendor detail page — mirrors Contact.primary_address. service_area stays a free-text
    # coverage descriptor, distinct from the postal address.
    address = models.ForeignKey(
        "addresses.Address", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    website = models.URLField(blank=True)
    usdot_number = models.CharField("USDOT number", max_length=40, blank=True)
    vehicle_types = models.ManyToManyField("leads.VehicleType", blank=True, related_name="vendors")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def insurance_status(self) -> str:
        """Worst status across this vendor's policies, or 'none' if there are none."""
        statuses = {p.status for p in self.policies.all()}
        for worst in INSURANCE_SEVERITY:
            if worst in statuses:
                return worst
        return "none"

    @property
    def needs_attention(self) -> bool:
        """True when coverage is lapsed, expiring within 30 days, or missing."""
        return self.insurance_status in INSURANCE_ATTENTION

    def insurance_summary(self) -> dict:
        """Status + human label for the governing (worst, then soonest) policy.
        Drives the directory insurance cell and the needs-attention strip."""
        policies = list(self.policies.all())
        if not policies:
            return {
                "status": "none",
                "days": None,
                "expiry": None,
                "label": "No coverage on file",
            }
        rank = {s: i for i, s in enumerate(INSURANCE_SEVERITY)}
        worst = min(policies, key=lambda p: (rank[p.status], p.expiry_date))
        days = worst.days_until_expiry
        if worst.status == "expired":
            n = abs(days)
            label = f"Lapsed {n} day{'s' if n != 1 else ''} ago"
        elif worst.status == "valid":
            label = f"Valid · exp {worst.expiry_date:%b '%y}"
        else:
            label = (
                "Expires today" if days == 0 else f"Expires in {days} day{'s' if days != 1 else ''}"
            )
        return {"status": worst.status, "days": days, "expiry": worst.expiry_date, "label": label}


class VendorDriver(TimeStampedModel):
    """A driver on a vendor's roster. Stays optional when a trip is later farmed out."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="drivers")
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    license_number = models.CharField(max_length=60, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class VendorDocument(TimeStampedModel):
    """A file attached to a vendor. created_at is the uploaded-on timestamp."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="documents")
    label = models.CharField(max_length=160)
    file = models.FileField(upload_to="vendor-docs/")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.label


class VendorInsurance(TimeStampedModel):
    """A liability-insurance policy on file for a vendor. Dates are plain calendar
    dates, compared against timezone.localdate() (no trip-timezone involved).

    Urgency is a graded ramp so the directory can escalate visually:
    valid > expiring(30) > urgent(15) > critical(10) > expired.
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

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="policies")
    insurer = models.CharField(max_length=200)
    policy_number = models.CharField(max_length=80, blank=True)
    coverage_amount = MoneyField()
    effective_date = models.DateField()
    expiry_date = models.DateField()
    certificate = models.FileField(upload_to="vendor-insurance/", blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-expiry_date"]

    def __str__(self) -> str:
        return f"{self.insurer} · {self.policy_number}".rstrip(" ·")

    @property
    def days_until_expiry(self) -> int:
        return (self.expiry_date - timezone.localdate()).days

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
