from django.db import models
from django.db.models import Q

from apps.core.choices import Channel
from apps.core.models import TimeStampedModel
from apps.core.phone import to_e164


class ContactManager(models.Manager):
    """Dedupe helpers — a Contact mirrors one LimoAnywhere Account."""

    def find_match(self, *, phone: str = "", email: str = "") -> "Contact | None":
        phone, email = (phone or "").strip(), (email or "").strip()
        lookup = Q()
        if phone:
            lookup |= Q(phone__iexact=phone)
        if email:
            lookup |= Q(email__iexact=email)
        if not lookup:
            return None
        return self.filter(lookup).order_by("-created_at").first()

    def match_or_create(
        self,
        *,
        name: str,
        company: str = "",
        phone: str = "",
        email: str = "",
        channel: str = Channel.WEBSITE,
    ) -> "Contact":
        existing = self.find_match(phone=phone, email=email)
        if existing is not None:
            return existing
        return self.create(
            name=name,
            company=company,
            phone=(phone or "").strip(),
            email=(email or "").strip(),
            channel=channel,
        )


class Contact(TimeStampedModel):
    """A customer (person or company) — the LimoAnywhere Account."""

    objects = ContactManager()

    name = models.CharField(max_length=200)
    company = models.CharField(max_length=200, blank=True)
    # Retained one release for rollback; superseded by the ContactPhone table.
    legacy_phone = models.CharField(max_length=32, blank=True, db_column="phone")
    email = models.EmailField(blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.WEBSITE)
    la_account_id = models.CharField("LimoAnywhere account", max_length=64, blank=True)
    podium_contact_uid = models.CharField("Podium contact UID", max_length=64, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.name} · {self.company}" if self.company else self.name

    @property
    def primary_phone(self) -> "ContactPhone | None":
        return self.phones.filter(is_primary=True).first() or self.phones.first()

    @property
    def phone(self) -> str:
        """Primary number in E.164, or "" — keeps every read site working unchanged."""
        primary = self.primary_phone
        return primary.e164 if primary else ""

    @phone.setter
    def phone(self, raw: str | None) -> None:
        """Buffer the value; `save()` flushes it once a PK exists."""
        self._pending_phone = raw or ""

    def save(self, *args, **kwargs) -> None:
        """Save, then flush any phone buffered by the `phone` setter.

        The `phone` setter can't create a `ContactPhone` row inline because it may run
        before this `Contact` has a PK (e.g. `Contact(phone=...)` then `.save()`) — there
        is no FK target yet. It stashes the raw value in `_pending_phone` instead; once
        `super().save()` guarantees a PK, we flush it through `set_primary_phone`.
        """
        super().save(*args, **kwargs)
        pending = getattr(self, "_pending_phone", None)
        if pending is not None:
            self._pending_phone = None
            self.set_primary_phone(pending)

    def _owned_or_unclaimed(self, e164: str) -> bool:
        """True unless `e164` is already attached to a *different* contact.

        `ContactPhone.e164` is globally unique, so a naive lookup-by-number would let
        `set_primary_phone`/`add_phone` silently reassign another contact's row to
        `self`. Product decision: refuse, never steal — no error, no reassignment.
        """
        owner = ContactPhone.objects.filter(e164=e164).values_list("contact_id", flat=True).first()
        return owner is None or owner == self.pk

    def set_primary_phone(self, raw: str) -> "ContactPhone | None":
        """Make `raw` this contact's primary number, demoting any current primary.

        Returns None (no-op) if `raw` cannot be normalized or already belongs to
        another contact.
        """
        e164 = to_e164(raw)
        if e164 is None:
            return None
        if not self._owned_or_unclaimed(e164):
            return None
        self.phones.exclude(e164=e164).update(is_primary=False)
        phone, _ = ContactPhone.objects.update_or_create(
            e164=e164,
            defaults={"contact": self, "is_primary": True},
        )
        return phone

    def add_phone(self, raw: str, label: str = "", primary: bool = False) -> "ContactPhone | None":
        """Attach another number.

        Returns None if it cannot be normalized, or if it already belongs to another
        contact (refuse, never steal — see `_owned_or_unclaimed`).
        """
        if primary:
            phone = self.set_primary_phone(raw)
            if phone is not None and label:
                phone.label = label
                phone.save(update_fields=["label", "updated_at"])
            return phone
        e164 = to_e164(raw)
        if e164 is None:
            return None
        if not self._owned_or_unclaimed(e164):
            return None
        phone, created = ContactPhone.objects.get_or_create(
            e164=e164,
            defaults={
                "contact": self,
                "label": label,
                "is_primary": not self.phones.exists(),
            },
        )
        return phone


class ContactPhone(TimeStampedModel):
    """One phone number. Mirrors Podium's `phoneNumbers[]` array.

    `e164` is globally unique — "one number belongs to one contact" is a database
    invariant, which makes dedupe a single indexed lookup.
    """

    contact = models.ForeignKey(Contact, related_name="phones", on_delete=models.CASCADE)
    e164 = models.CharField(max_length=20, unique=True, db_index=True)
    label = models.CharField(max_length=20, blank=True)
    is_primary = models.BooleanField(default=False)
    opted_out = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "created_at"]

    def __str__(self) -> str:
        return f"{self.e164} ({self.label})" if self.label else self.e164
