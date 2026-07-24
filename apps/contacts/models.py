from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from apps.core.choices import Channel
from apps.core.models import TimeStampedModel
from apps.core.phone import to_e164


class CompanyManager(models.Manager):
    def get_or_create_by_name(self, name: str) -> "Company | None":
        """Resolve a typed company name to a Company (case-insensitive), or None if blank."""
        name = (name or "").strip()
        if not name:
            return None
        existing = self.filter(name__iexact=name).first()
        if existing is not None:
            return existing
        return self.create(name=name)


class Company(TimeStampedModel):
    """A reusable organization a Contact can belong to (CRM Account)."""

    objects = CompanyManager()

    name = models.CharField(max_length=200)
    billing_contact = models.ForeignKey(
        "contacts.Contact",
        related_name="billed_companies",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The person billed for this company's bookings.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("name"), name="uniq_company_name_ci"),
        ]
        verbose_name_plural = "companies"

    def __str__(self) -> str:
        return self.name


class ContactManager(models.Manager):
    """Dedupe helpers — a Contact mirrors one LimoAnywhere Account."""

    def find_match(self, *, phone: str = "", email: str = "") -> "Contact | None":
        phone, email = (phone or "").strip(), (email or "").strip()
        lookup = Q()
        if phone:
            # Match the canonical form *and* the raw input: rows that predate the
            # backfill, and numbers to_e164 rejects, are only reachable as typed.
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
        company_name: str = "",
        phone: str = "",
        email: str = "",
        channel: str = Channel.WEBSITE,
    ) -> "Contact":
        existing = self.find_match(phone=phone, email=email)
        if existing is not None:
            return existing
        from apps.contacts.models import Company  # local import avoids ordering issues

        return self.create(
            name=name,
            company=Company.objects.get_or_create_by_name(company_name),
            phone=to_e164(phone) or (phone or "").strip(),
            email=(email or "").strip(),
            channel=channel,
        )


class Contact(TimeStampedModel):
    """A customer (person or company) — the LimoAnywhere Account."""

    objects = ContactManager()

    name = models.CharField(max_length=200)
    company = models.ForeignKey(
        "contacts.Company",
        related_name="contacts",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    phone = models.CharField(max_length=32, blank=True)
    # null=True is intentional: blank saves as NULL so the case-insensitive unique
    # constraint below allows any number of contacts with no email.
    email = models.EmailField(blank=True, null=True)  # noqa: DJ001
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.WEBSITE)
    la_account_id = models.CharField("LimoAnywhere account", max_length=64, blank=True)
    podium_contact_uid = models.CharField("Podium contact UID", max_length=64, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="uniq_contact_email_ci"),
        ]

    def __str__(self) -> str:
        return f"{self.name} · {self.company.name}" if self.company else self.name

    def save(self, *args, **kwargs):
        # Blank email → NULL (many allowed); otherwise store lowercase for CI uniqueness.
        self.email = (self.email or "").strip().lower() or None
        super().save(*args, **kwargs)
