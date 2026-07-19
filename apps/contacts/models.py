import logging
import re

from django.db import models

from apps.core.choices import Channel
from apps.core.models import TimeStampedModel
from apps.core.phone import is_phone_like, to_e164

logger = logging.getLogger(__name__)


class ContactManager(models.Manager):
    """Dedupe helpers — a Contact mirrors one LimoAnywhere Account.

    Single canonical match order for the whole system: Podium uid, then any of the
    contact's phone numbers (normalized), then email. Both the New-lead modal and the
    Podium webhook go through here — they used to disagree about what "same person" means.
    """

    def find_match(
        self, *, phone: str = "", email: str = "", podium_uid: str = ""
    ) -> "Contact | None":
        """Find the contact identified by `podium_uid`, then `phone`, then `email`."""
        if podium_uid:
            match = self.filter(podium_contact_uid=podium_uid).first()
            if match is not None:
                return match

        e164 = to_e164(phone)
        if e164:
            match = self.filter(phones__e164=e164).first()
            if match is not None:
                return match

        email = (email or "").strip()
        if email:
            return self.filter(email__iexact=email).order_by("-created_at").first()
        return None

    def match_or_create(
        self,
        *,
        name: str,
        company: str = "",
        phone: str = "",
        email: str = "",
        channel: str = Channel.WEBSITE,
        podium_uid: str = "",
    ) -> "Contact":
        """Return the matching contact (enriched with any new phone/uid), or create one."""
        existing = self.find_match(phone=phone, email=email, podium_uid=podium_uid)
        if existing is not None:
            self._enrich(existing, phone=phone, podium_uid=podium_uid)
            return existing
        return self.create(
            name=name,
            company=company,
            phone=phone,
            email=(email or "").strip(),
            channel=channel,
            podium_contact_uid=podium_uid,
        )

    def _enrich(self, contact: "Contact", *, phone: str, podium_uid: str) -> None:
        """Backfill what we just learned onto a matched contact, without overwriting."""
        if podium_uid and not contact.podium_contact_uid:
            contact.podium_contact_uid = podium_uid
            contact.save(update_fields=["podium_contact_uid", "updated_at"])
        e164 = to_e164(phone)
        if e164 and not contact.phones.filter(e164=e164).exists():
            contact.add_phone(e164)


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
        """Primary number if any, else the first by creation.

        Iterates the already-loaded `phones` queryset in Python rather than calling
        `.filter()` on the related manager — `.filter()` on a related manager bypasses
        `prefetch_related`, turning a directory listing into one query per row.
        """
        phones = list(self.phones.all())
        for p in phones:
            if p.is_primary:
                return p
        return phones[0] if phones else None

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

    def _other_owner(self, e164: str) -> int | None:
        """Pk of a *different* contact that already owns `e164`, or None if it's
        unclaimed or already `self`'s.

        `ContactPhone.e164` is globally unique, so a naive lookup-by-number would let
        `set_primary_phone`/`add_phone` silently reassign another contact's row to
        `self`. Product decision: refuse, never steal — no error, no reassignment.
        """
        owner = ContactPhone.objects.filter(e164=e164).values_list("contact_id", flat=True).first()
        return owner if owner is not None and owner != self.pk else None

    def _log_refusal(self, e164: str, other_owner: int) -> None:
        logger.info(
            "Refusing to reassign phone %s from contact %s to contact %s (refuse, never steal)",
            e164,
            other_owner,
            self.pk,
        )

    def set_primary_phone(self, raw: str) -> "ContactPhone | None":
        """Make `raw` this contact's primary number, demoting any current primary.

        Returns None (no-op) if `raw` cannot be normalized or already belongs to
        another contact.
        """
        e164 = to_e164(raw)
        if e164 is None:
            return None
        other_owner = self._other_owner(e164)
        if other_owner is not None:
            self._log_refusal(e164, other_owner)
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
        contact (refuse, never steal — see `_other_owner`).
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
        other_owner = self._other_owner(e164)
        if other_owner is not None:
            self._log_refusal(e164, other_owner)
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


class ContactPhoneQuerySet(models.QuerySet):
    """QuerySet-level helpers for `ContactPhone` — shared search entry point."""

    def matching(self, term: str) -> "ContactPhoneQuerySet":
        """Numbers whose E.164 contains the digits of a free-text search `term`.

        Returns `.none()` when `term` isn't phone-like (see `is_phone_like`) — e.g. a
        search like "Suite 5" or "3rd Ave Limo" must NOT collapse to digit "5" / "3"
        and match nearly every phone number in the system via `icontains`. This is the
        single canonical implementation of "is this search term a phone search" so the
        contacts directory and inbox search can't drift on the rule.

        Returns a queryset of `ContactPhone` (not `.values()`) — callers that need a
        `contact_id__in=...` / `pk__in=...` subquery (to avoid multiplying aggregate
        annotations on the outer queryset) should call `.values("contact_id")` on the
        result themselves.
        """
        if not is_phone_like(term):
            return self.none()
        digits = re.sub(r"\D", "", term)
        return self.filter(e164__icontains=digits)


class ContactPhone(TimeStampedModel):
    """One phone number. Mirrors Podium's `phoneNumbers[]` array.

    `e164` is globally unique — "one number belongs to one contact" is a database
    invariant, which makes dedupe a single indexed lookup.
    """

    objects = ContactPhoneQuerySet.as_manager()

    contact = models.ForeignKey(Contact, related_name="phones", on_delete=models.CASCADE)
    e164 = models.CharField(max_length=20, unique=True, db_index=True)
    label = models.CharField(max_length=20, blank=True)
    is_primary = models.BooleanField(default=False)
    opted_out = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "created_at"]

    def __str__(self) -> str:
        return f"{self.e164} ({self.label})" if self.label else self.e164
