from django.db import models

from apps.core.models import TimeStampedModel


class Address(TimeStampedModel):
    """A postal address + LocationIQ metadata. Shared, reusable across hosts (Contact now)."""

    landmark_name = models.CharField(max_length=160, blank=True)
    line1 = models.CharField(max_length=200, blank=True)
    line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=80, blank=True)
    postal = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True, default="United States")

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    locationiq_place_id = models.CharField(max_length=64, blank=True)
    place_type = models.CharField(max_length=60, blank=True)
    place_class = models.CharField(max_length=60, blank=True)
    display_name = models.CharField(max_length=300, blank=True)

    _USER_FIELDS = ("landmark_name", "line1", "line2", "city", "state", "postal")

    @property
    def is_blank(self) -> bool:
        """True when the user-facing fields are all empty (country has a default, ignore it)."""
        return not any(getattr(self, f) for f in self._USER_FIELDS)

    def __str__(self) -> str:
        return self.landmark_name or self.line1 or self.display_name or "Address (empty)"
