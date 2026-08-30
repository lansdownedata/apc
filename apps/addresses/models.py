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


class Airport(TimeStampedModel):
    """A US passenger airport, seeded from OurAirports data (see apps/addresses/data/).

    Searched alongside LocationIQ so airports rank above street addresses in every
    address input. The CSV's coordinates are authoritative — `enrich_airports` may
    add a street line and place id, but must never touch latitude/longitude.

    Two independent concerns, deliberately kept as separate flags rather than one axis
    (2026-08-29 data expansion): `serves_ground_transport` is "can a car be sent here" —
    true for the original 863 curated rows (DC-area + the client's usual destinations,
    plus US territories) and false for the global set added only so a flight's other
    endpoint (e.g. a London arrival) has a resolvable name. `has_scheduled_service` is
    "do scheduled passenger flights exist here" — false for 391 of the curated 863
    (military fields, GA relievers, private strips like Andrews or Manassas) that are
    legitimate pickups but can never be looked up with a flight number.
    """

    class Size(models.TextChoices):
        LARGE = "large_airport", "Large"
        MEDIUM = "medium_airport", "Medium"

    ourairports_id = models.PositiveIntegerField(unique=True)
    ident = models.CharField(max_length=8, unique=True, db_index=True)
    # The source sheet's `iata_code`, verbatim. For small fields this is a local
    # ident rather than a real IATA code (e.g. "07FA", "67L") — matching treats it
    # as a code either way, which is what a user typing it expects.
    iata = models.CharField(max_length=8, blank=True, db_index=True)
    icao = models.CharField(max_length=8, blank=True, db_index=True)
    size = models.CharField(max_length=20, choices=Size.choices)
    name = models.CharField(max_length=120, db_index=True)
    city = models.CharField(max_length=80, db_index=True)
    state = models.CharField(max_length=2)
    country = models.CharField(max_length=2, default="US")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=10, decimal_places=6)
    elevation_ft = models.IntegerField(null=True, blank=True)
    # IANA zone, e.g. "America/New_York" — filled from the CSV's `timezone` column (spec
    # 2026-08-29 §4.1). Flight times arrive airport-local and are stored UTC via this.
    timezone = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    # See the class docstring — these two are independent axes, not one flag.
    serves_ground_transport = models.BooleanField(default=False)
    has_scheduled_service = models.BooleanField(default=False)

    # LocationIQ enrichment — blank until `manage.py enrich_airports` runs.
    locationiq_place_id = models.CharField(max_length=64, blank=True)
    line1 = models.CharField(max_length=200, blank=True)
    postal = models.CharField(max_length=20, blank=True)
    display_name = models.CharField(max_length=300, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    @property
    def label(self) -> str:
        """Dropdown main line: 'DCA — Ronald Reagan Washington National Airport'."""
        return f"{self.iata} — {self.name}" if self.iata else self.name

    def __str__(self) -> str:
        return self.label


class Airline(TimeStampedModel):
    """A carrier that can be attached to an airport stop.

    Seeded from `apps/addresses/data/airlines.csv` (`iata,icao,name`) by migration 0004
    and `manage.py seed_airlines`. Retire a carrier with `is_active=False` rather than
    deleting it — stops keep a PROTECT link to the airline they were booked on.
    """

    iata = models.CharField(max_length=3, unique=True)
    icao = models.CharField(max_length=4, blank=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    @property
    def label(self) -> str:
        """Picker line: 'UA — United Airlines'."""
        return f"{self.iata} — {self.name}"

    def __str__(self) -> str:
        return self.label
