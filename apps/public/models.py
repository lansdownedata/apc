"""Public-site models.

`apps.public` is otherwise a presentation app — the marketing site and the booking
funnel — and deliberately stores nothing. SlotHold is here rather than in
apps.integrations because it is about OUR visitors colliding with each other, not
about Calendly: swap the calendar provider and the hold logic is unchanged.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.core.models import TimeStampedModel

# How long a visitor keeps a slot after clicking it. Short on purpose: the cost of an
# abandoned form is a slot greyed out for everyone else.
DEFAULT_HOLD_MINUTES = 10


def _hold_minutes() -> int:
    return int(getattr(settings, "CALENDLY_HOLD_MINUTES", DEFAULT_HOLD_MINUTES))


class SlotHoldQuerySet(models.QuerySet):
    def active(self) -> "SlotHoldQuerySet":
        return self.filter(expires_at__gt=timezone.now())


class SlotHoldManager(models.Manager.from_queryset(SlotHoldQuerySet)):
    """Claim/release, with the concurrency handled in `claim` rather than by an index.

    There is exactly ONE row per start_time, recycled as the slot changes hands. The
    obvious alternative — a conditional UniqueConstraint on "active holds" — is not
    available: prod is Postgres (partial indexes) while local and test are MySQL
    (none), so it would be enforced in production and silently missing everywhere it
    could be caught. A total unique constraint behaves identically on both.
    """

    def claim(self, start_time: datetime, session_key: str) -> "SlotHold | None":
        """Hold the slot for this session, or None if someone else already has it.

        Re-claiming your own live hold extends it and returns it — a visitor who
        double-clicks submit must not lock themselves out of their own slot.
        """
        expires_at = timezone.now() + timedelta(minutes=_hold_minutes())
        try:
            with transaction.atomic():
                hold = self.select_for_update().filter(start_time=start_time).first()
                if hold is None:
                    return self.create(
                        start_time=start_time, session_key=session_key, expires_at=expires_at
                    )
                if hold.expires_at > timezone.now() and hold.session_key != session_key:
                    return None
                hold.session_key = session_key
                hold.expires_at = expires_at
                hold.save(update_fields=["session_key", "expires_at", "updated_at"])
                return hold
        except IntegrityError:
            # Another request created the row between the SELECT and the INSERT. It got
            # there first, so it owns the slot — the unique constraint is what decides,
            # not who read first.
            return None

    def release(self, start_time: datetime, session_key: str) -> None:
        """Give the slot back, but only to whoever holds it.

        Scoped to the session so a visitor cannot free someone else's slot simply by
        posting its start time.
        """
        self.filter(start_time=start_time, session_key=session_key).delete()

    def held_start_times(self) -> set[datetime]:
        """Live holds, for greying out slots in the grid."""
        return set(self.active().values_list("start_time", flat=True))


class SlotHold(TimeStampedModel):
    """An advisory claim on one Calendly slot while a visitor completes the form.

    Never consulted as proof a slot is free — see the module docstring and the plan's
    decision 6. `already_filled` from Calendly is the only authority.
    """

    start_time = models.DateTimeField(unique=True, help_text="Slot start, UTC.")
    session_key = models.CharField(max_length=64)
    expires_at = models.DateTimeField(db_index=True)

    objects = SlotHoldManager()

    class Meta:
        ordering = ["start_time"]

    def __str__(self) -> str:
        return f"{self.start_time:%Y-%m-%d %H:%M} UTC held by {self.session_key[:8]}"


# The exact wording shown beside the opt-in checkbox. Defined once and rendered from
# here (via the site_settings context processor) so the page and the stored record can
# never drift — a consent record that misquotes what was on screen is worth nothing.
SMS_CONSENT_TEXT = (
    "Yes — All Pro Charter may call and text me about this meeting at the number "
    "above. Calendly sends the reminders; message and data rates may apply."
)


class BookingConsentManager(models.Manager):
    def record(
        self,
        *,
        name: str,
        email: str,
        phone: str,
        start_time: datetime,
        ip_address: str = "",
    ) -> "BookingConsent":
        """Write the consent down at the moment it is given.

        Stores the wording VERBATIM rather than a reference to it. The copy on the page
        will change; a record pointing at whatever the current text happens to be would
        misdescribe what somebody agreed to two years earlier, which is exactly when it
        would matter.
        """
        return self.create(
            name=name,
            email=email,
            phone=phone,
            start_time=start_time,
            ip_address=ip_address or "",
            consent_text=SMS_CONSENT_TEXT,
        )


class BookingConsent(TimeStampedModel):
    """One visitor's opt-in to being called and texted about a call they booked.

    Written only when the box was ticked AND the booking actually succeeded — a consent
    record for a meeting that never existed is noise in the one place that has to be
    trustworthy. Never updated and never deleted by the app.

    The booking endpoint otherwise persists nothing (lead creation belongs to the
    webhook), so this is the deliberate exception: the tick is worthless as evidence
    without a record of who gave it, when, from where, and to what wording.
    """

    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=32, help_text="E.164, as sent to Calendly.")
    start_time = models.DateTimeField(help_text="The meeting consented about, UTC.")
    consent_text = models.TextField(help_text="The wording shown, captured verbatim.")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    objects = BookingConsentManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.email} consented {self.created_at:%Y-%m-%d %H:%M} UTC"
