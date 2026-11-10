"""The Settings menu: one definition for the sidebar inside Settings.

`match` holds URL-name prefixes checked against `request.resolver_match.url_name`, so an
item stays current on its create/edit/delete pages too.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    label: str
    url_name: str
    icon: str  # Tabler class, e.g. "ti-car"
    keywords: str = ""
    match: tuple[str, ...] = ()  # url_name prefixes


@dataclass(frozen=True)
class Section:
    label: str | None
    items: tuple[Item, ...]


_OVERVIEW = Section(
    None,
    (
        Item(
            "Overview",
            "settings_index",
            "ti-layout-dashboard",
            "status attention",
            ("settings_index",),
        ),
    ),
)

_BOOKING = Section(
    "Booking",
    (
        Item(
            "Vehicle types",
            "vehicle_type_list",
            "ti-car",
            "fleet capacity photos cleaning fee hold rate card minimum hours",
            ("vehicle_type_",),
        ),
        Item(
            "Service types",
            "service_type_list",
            "ti-tag",
            "airport wedding corporate trip purpose",
            ("service_type_",),
        ),
        Item(
            "Points of interest",
            "poi_list",
            "ti-map-pin",
            "venues hotels ceremony sites vehicle cap",
            ("poi_",),
        ),
        Item(
            "Pricing",
            "pricing",
            "ti-calculator",
            "vendor share cost ratio affiliate rate margin",
            ("pricing",),
        ),
    ),
)

_OPERATIONS = Section(
    "Operations",
    (
        Item(
            "Dispatch alerts",
            "dispatch_alerts",
            "ti-bell-exclamation",
            "monitor thresholds milestones en route arrived sms",
            ("dispatch_alerts",),
        ),
        Item(
            "Tasks",
            "task_settings",
            "ti-checklist",
            "owners departments escalation overdue",
            ("task_settings",),
        ),
        Item(
            "Checklist schedule",
            "checklist_schedule",
            "ti-calendar-time",
            "due dates deposits insurance",
            ("checklist_schedule",),
        ),
    ),
)

_AFFILIATES = Section(
    "Affiliates & fleet",
    (
        Item(
            "Affiliate documents",
            "document_type_list",
            "ti-files",
            "paperwork required insurance coi",
            ("document_type_",),
        ),
        Item(
            "Renewal types",
            "renewal_type_list",
            "ti-calendar-check",
            "licenses registration inspections permits expiry",
            ("renewal_type_",),
        ),
        Item(
            "Affiliate scoring",
            "affiliate_scoring",
            "ti-chart-bar",
            "reliability score weights",
            ("affiliate_scoring",),
        ),
    ),
)

_MESSAGING = Section(
    "Messaging",
    (
        Item(
            "Messages",
            "messages",
            "ti-message-2",
            "touch-points templates sms email quiet hours timing",
            ("messages", "message_"),
        ),
    ),
)

_QUICKBOOKS = Item(
    "QuickBooks",
    "quickbooks_settings",
    "ti-building-bank",
    "qbo accounting invoices terms sync",
    ("quickbooks_",),
)


def sections() -> list[Section]:
    """The menu, top to bottom. QuickBooks shows only while its keys are set."""
    from apps.integrations import qbo

    menu = [_OVERVIEW, _BOOKING, _OPERATIONS, _AFFILIATES, _MESSAGING]
    if qbo.enabled():
        menu.append(Section("Integrations", (_QUICKBOOKS,)))
    return menu


def current_item(url_name: str | None) -> Item | None:
    """The first item one of whose `match` prefixes `url_name` starts with."""
    if not url_name:
        return None
    for section in sections():
        for item in section.items:
            if url_name.startswith(item.match):
                return item
    return None
