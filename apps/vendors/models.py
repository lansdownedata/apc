from django.conf import settings
from django.db import models
from django.db.models import Q

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
    location_name = models.CharField("location label", max_length=120, blank=True)
    address_line1 = models.CharField("street address", max_length=200, blank=True)
    unit = models.CharField("unit / suite", max_length=60, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=60, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)

    website = models.URLField(blank=True)
    usdot_number = models.CharField("USDOT number", max_length=40, blank=True)
    vehicle_types = models.ManyToManyField("leads.VehicleType", blank=True, related_name="vendors")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


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
