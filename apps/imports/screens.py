"""The two screens that import and export workbooks (APC-141, spec §3.6, §4.2, §4.4, §10).

Two entries, not a registry: each maps a URL slug to its resource, sheet, directory selector,
filters, accepted values and template examples.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.db.models import QuerySet
from django.db.models.functions import Lower
from django.http import Http404

from apps.contacts import selectors as contact_selectors
from apps.contacts.models import ContactPhone
from apps.contacts.resources import ContactResource
from apps.core.choices import Channel
from apps.leads.models import VehicleType
from apps.vendors import selectors as vendor_selectors
from apps.vendors.models import Vendor
from apps.vendors.resources import VendorResource

from .resources import PortalResource
from .xlsx import SheetSpec

RULES = (
    "Name is required for new rows.",
    "Rows are matched by Email, else by Phone.",
    "A row with neither an email nor a phone can't be imported.",
    "A blank cell leaves the saved value alone — an import never clears anything.",
    "Separate several values with commas.",
    "Rows with an @example.com email are examples and are skipped.",
)

Accepted = tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class Screen:
    slug: str
    noun: str
    resource: Callable[[], PortalResource]
    filter_keys: tuple[str, ...]
    sheet: str
    directory: Callable[..., QuerySet]
    clean_choice: Callable[[str | None], str]  # normalises the first filter key
    accepted: Callable[[], Accepted]
    example_rows: Callable[[], list[list[str]]]

    @property
    def columns(self):
        return self.resource.COLUMNS

    def spec(self) -> SheetSpec:
        """What the writer needs; the accepted values are read now, not at import time."""
        return SheetSpec(self.sheet, self.columns, self.accepted(), RULES)

    def clean_filters(self, raw) -> dict[str, str]:
        """The screen's filters as the list applies them: choice normalised, search stripped."""
        choice, *rest = self.filter_keys
        cleaned = {choice: self.clean_choice(raw.get(choice))}
        return cleaned | {key: (raw.get(key) or "").strip() for key in rest}

    def queryset(self, filters) -> QuerySet:
        """The rows the screen shows for `filters` (as `clean_filters` returns them), joined
        for the export, ordered by name."""
        rows = self.directory(*(filters[key] for key in self.filter_keys))
        return self.resource.export_queryset(rows).order_by(Lower("name"), "pk")

    def examples(self) -> list[list[str]]:
        return self.example_rows()


def _contact_accepted() -> Accepted:
    return (("Channel", tuple(Channel.labels)), ("Phone label", tuple(ContactPhone.Label.labels)))


def _active_type_names() -> tuple[str, ...]:
    active = VehicleType.objects.filter(active=True).order_by("sort_order", "name")
    return tuple(active.values_list("name", flat=True))


def _vendor_accepted() -> Accepted:
    statuses = tuple(str(label) for label in Vendor.Status.labels)
    return (("Status", statuses), ("Vehicle types", _active_type_names()))


def _contact_examples() -> list[list[str]]:
    return [
        [
            "Jordan Example",
            "jordan@example.com",
            "(202) 555-0143",
            "(202) 555-0199 (Work)",
            "Example Events LLC",
            "Website",
            "1600 Pennsylvania Ave NW",
            "",
            "Washington",
            "DC",
            "20500",
            "Prefers the Escalade",
        ],
        ["Sam Example", "sam@example.com", *[""] * 10],
    ]


def _vendor_examples() -> list[list[str]]:
    """One full row; its vehicle types are real active ones, so the example would validate."""
    return [
        [
            "Example Limousine",
            "Pat Example",
            "dispatch@example.com",
            "(202) 555-0143",
            "Active",
            ", ".join(_active_type_names()[:2]),
            "Washington DC metro",
            "https://example.com",
            "1234567",
            "1600 Pennsylvania Ave NW",
            "",
            "Washington",
            "DC",
            "20500",
            "Prefers 48 hours' notice",
        ]
    ]


SCREENS: dict[str, Screen] = {
    "contacts": Screen(
        slug="contacts",
        noun="contacts",
        resource=ContactResource,
        filter_keys=("scope", "q"),
        sheet="Contacts",
        directory=contact_selectors.directory,
        clean_choice=contact_selectors.clean_scope,
        accepted=_contact_accepted,
        example_rows=_contact_examples,
    ),
    "vendors": Screen(
        slug="vendors",
        noun="vendors",
        resource=VendorResource,
        filter_keys=("status", "q"),
        sheet="Vendors",
        directory=vendor_selectors.directory,
        clean_choice=vendor_selectors.clean_status,
        accepted=_vendor_accepted,
        example_rows=_vendor_examples,
    ),
}


def get_screen(slug: str) -> Screen:
    """The screen for a URL slug; anything else is a 404."""
    try:
        return SCREENS[slug]
    except KeyError:
        raise Http404("No such import screen") from None
