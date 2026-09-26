import re

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

    def search(self, query: str) -> models.QuerySet:
        """Free-text lookup over name / company / email / phone.

        Shared by the contacts directory and the booking modal's type-ahead so the two
        agree on what "matches" means. Phones are stored E.164 (+16175559271), so match
        on digits — a query typed as "(617) 555-9271" or "555-9271" still finds them.
        """
        query = (query or "").strip()
        if not query:
            return self.none()
        lookup = (
            Q(name__icontains=query) | Q(company__name__icontains=query) | Q(email__icontains=query)
        )
        phone_digits = re.sub(r"\D", "", query)
        if len(phone_digits) >= 3:
            lookup |= Q(phone__icontains=phone_digits) | Q(
                pk__in=ContactPhone.objects.filter(number__icontains=phone_digits).values("contact")
            )
        return self.filter(lookup)

    def find_match(self, *, phone: str = "", email: str = "") -> "Contact | None":
        """The contact a returning customer's details belong to.

        Email is the key: when there is one, it alone decides — a known phone on a new
        email is a different person (family members share numbers). Only with no email
        (a Podium text, a phone-only entry) does the phone match, against every number
        on file rather than just the texting one.
        """
        phone, email = (phone or "").strip(), (email or "").strip()
        if email:
            return self.filter(email__iexact=email).first()
        if not phone:
            return None
        # Match the canonical form *and* the raw input: rows that predate the
        # backfill, and numbers to_e164 rejects, are only reachable as typed.
        numbers = {phone, to_e164(phone) or phone}
        lookup = Q(phone__in=numbers) | Q(
            pk__in=ContactPhone.objects.filter(number__in=numbers).values("contact")
        )
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
        """Find the customer by `find_match`, or create them.

        An email match takes the name they gave this time (the most recent one wins; a
        phone-only match never renames — shared numbers). Any new phone is added as an
        extra number — never a swap, so the number Podium texts only
        changes when an agent says so. Company and channel are left as they were.
        """
        from apps.contacts import services  # local import: services imports this module

        existing = self.find_match(phone=phone, email=email)
        if existing is None:
            return self.create(
                name=name,
                company=Company.objects.get_or_create_by_name(company_name),
                phone=to_e164(phone) or (phone or "").strip(),
                email=(email or "").strip(),
                channel=channel,
            )
        name = (name or "").strip()
        if (email or "").strip() and name and name != existing.name:
            existing.name = name
            existing.save(update_fields=["name", "updated_at"])
        if (phone or "").strip():
            try:
                services.add_phone(existing, phone)
            except services.PhoneError:
                pass  # an undiallable extra number is not worth failing a booking over
        return existing


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
    primary_address = models.ForeignKey(
        "addresses.Address", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    billing_address = models.ForeignKey(
        "addresses.Address", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    billing_same_as_primary = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="uniq_contact_email_ci"),
        ]

    def __str__(self) -> str:
        return f"{self.name} · {self.company.name}" if self.company else self.name

    @property
    def effective_billing_address(self):
        """Billing address, falling back to the primary when 'same as primary' is set."""
        return self.primary_address if self.billing_same_as_primary else self.billing_address

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._saved_phone = instance.__dict__.get("phone")
        return instance

    def save(self, *args, **kwargs):
        # Blank email → NULL (many allowed); otherwise store lowercase for CI uniqueness.
        self.email = (self.email or "").strip().lower() or None
        super().save(*args, **kwargs)
        if self.phone != getattr(self, "_saved_phone", None):
            self._sync_texting_phone()
            self._saved_phone = self.phone

    def _sync_texting_phone(self) -> None:
        """Keep the ContactPhone rows agreeing with `phone`, the number Podium texts.

        Every path that writes `phone` (the lead header, the New-lead modal, a Podium
        contact, the admin) lands here, so the old number is kept as an extra rather
        than lost, and exactly one row is marked texting.
        """
        self.phones.filter(texting=True).exclude(number=self.phone).update(texting=False)
        if self.phone:
            row, created = self.phones.get_or_create(number=self.phone, defaults={"texting": True})
            if not created and not row.texting:
                self.phones.filter(pk=row.pk).update(texting=True)


class ContactPhone(TimeStampedModel):
    """One number on file for a contact. The `texting` one is mirrored on `Contact.phone`.

    Manage them through `apps.contacts.services` (add / update / use for texting /
    remove), which keeps the mirror right.
    """

    class Label(models.TextChoices):
        MOBILE = "mobile", "Mobile"
        WORK = "work", "Work"
        HOME = "home", "Home"
        OTHER = "other", "Other"

    contact = models.ForeignKey(Contact, related_name="phones", on_delete=models.CASCADE)
    number = models.CharField(max_length=32)
    label = models.CharField(max_length=10, choices=Label.choices, default=Label.MOBILE)
    texting = models.BooleanField(
        default=False, help_text="The number Podium texts. One per contact."
    )

    class Meta:
        ordering = ["-texting", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["contact", "number"], name="uniq_contact_phone"),
        ]

    def __str__(self) -> str:
        return f"{self.get_label_display()} {self.number}"
