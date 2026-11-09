"""The History drawer's fragment, selector and staff URLs (spec §6.1, §6.2, §8; §9 group 8)."""

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from auditlog.context import disable_auditlog
from django.db import connection
from django.template.loader import render_to_string
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.audit.children import track_children
from apps.audit.context import audit_context
from apps.audit.factories import AuditEntryFactory
from apps.audit.models import AuditEntry
from apps.billing.factories import AccountGroupFactory, BillingAccountFactory, InvoiceFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import ContactPhone
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.payments.models import AffiliatePayable
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import Reservation, Stop
from apps.vendors.factories import VendorDriverFactory, VendorFactory

pytestmark = pytest.mark.django_db

NY = "America/New_York"
LA = "America/Los_Angeles"


@pytest.fixture
def staff(client):
    client.force_login(UserFactory(first_name="Jane", last_name="Doe"))
    return client


def _portal(user=None):
    return audit_context(actor=user, source="portal")


def _quiet_trip(**kwargs) -> Reservation:
    """A trip whose own creation writes nothing, so a test controls every entry."""
    with disable_auditlog():
        return ReservationFactory(**kwargs)


def _get(client, kind: str, pk: int, **params):
    name = kind if kind in ("trip", "order") else "profile"
    args = [pk] if name != "profile" else [kind, pk]
    return client.get(reverse(f"audit:{name}", args=args), params)


# --- access ---


@pytest.mark.parametrize("kind", ["trip", "order", "contact"])
def test_login_required(client, kind):
    trip = ReservationFactory()
    pk = {"trip": trip.pk, "order": trip.lead_id, "contact": trip.lead.contact_id}[kind]
    resp = _get(client, kind, pk)
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


def test_unknown_kind_404(staff):
    lead = LeadFactory()
    assert staff.get(f"/portal/history/lead/{lead.pk}/").status_code == 404
    assert staff.get(f"/portal/history/planner/{lead.pk}/").status_code == 404


def test_missing_anchor_404(staff):
    assert _get(staff, "trip", 987_654).status_code == 404
    assert _get(staff, "vendor", 987_654).status_code == 404


# --- what each panel holds ---


def test_trip_fragment_has_its_extras_assignment_route_not_sibling(staff):
    lead = LeadFactory()
    trip = ReservationFactory(lead=lead)
    sibling = ReservationFactory(lead=lead)
    with _portal():
        TripExtraFactory(reservation=trip, label="Tolls", amount=25)
        AssignmentFactory(reservation=trip)
        with track_children(trip, route=True):
            Stop.objects.filter(reservation=trip, sequence=1).update(address="Logan Airport")
        TripExtraFactory(reservation=sibling, label="Parking", amount=12)

    resp = _get(staff, "trip", trip.pk)

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Added extra Tolls $25.00" in body
    assert "Added Vendor assignment" in body
    assert "Stop 2 address" in body
    assert "Logan Airport" in body
    assert "Parking" not in body


def test_order_fragment_has_order_and_all_trips_with_prefix(staff):
    lead = LeadFactory()
    airport = ServiceTypeFactory(name="Airport transfer")  # get_or_create: may be titlecased
    wedding = ServiceTypeFactory(name="Wedding")
    first = ReservationFactory(lead=lead, pickup_date=date(2026, 11, 14), service_type=airport)
    second = ReservationFactory(lead=lead, pickup_date=date(2026, 11, 20), service_type=wedding)
    other = ReservationFactory()
    with _portal():
        lead.notes = "Prefers texts"
        lead.save()
        TripExtraFactory(reservation=first, label="Tolls", amount=25)
        TripExtraFactory(reservation=second, label="Champagne", amount=80)
        TripExtraFactory(reservation=other, label="Parking", amount=12)

    resp = _get(staff, "order", lead.pk)

    body = resp.content.decode()
    assert "Prefers texts" in body
    assert f"Nov 14 · {airport.name}" in body
    assert "Nov 20 · Wedding" in body
    assert "Added extra Tolls $25.00" in body
    assert "Added extra Champagne $80.00" in body
    assert "Parking" not in body


def test_order_trip_lines_in_their_trips_zone_headings_in_the_earliest(staff):
    """Ruling 19/20: a trip's times read in its own zone and on its pickup date's
    abbreviation; the day heading is in the earliest trip's zone."""
    lead = LeadFactory()
    with disable_auditlog():
        ReservationFactory(lead=lead, pickup_date=date(2025, 11, 14), pickup_timezone=NY)
        west = ReservationFactory(lead=lead, pickup_date=date(2025, 11, 20), pickup_timezone=LA)
    # 07:30Z on Nov 15 is still Nov 14 in Los Angeles, already Nov 15 in New York.
    AuditEntryFactory(
        object_id=west.pk,
        trip_id=west.pk,
        order_id=lead.pk,
        changes={"pickup_time": ["19:30:00", "20:00:00"]},
        timestamp=datetime(2025, 11, 15, 7, 30, tzinfo=UTC),
    )

    body = _get(staff, "order", lead.pk).content.decode()

    assert "7:30 PM PST" in body
    assert "8:00 PM PST" in body
    assert "11:30 PM PST" in body  # the group's clock, in its trip's zone
    assert "Sat Nov 15, 2025" in body
    assert "Nov 14, 2025" not in body


def test_contact_profile_fragment_includes_phone_billing_invoice_entries(staff):
    contact = ContactFactory()
    stranger = ContactFactory()
    with _portal():
        ContactPhone.objects.create(contact=contact, number="+12025550134")
        account = BillingAccountFactory(contact=contact, name="Ridgeline Partners")
        invoice = InvoiceFactory(group=AccountGroupFactory(account=account, name="Ops team"))
        ContactPhone.objects.create(contact=stranger, number="+12025550999")

    body = _get(staff, "contact", contact.pk).content.decode()

    assert "+12025550134" in body
    assert "Ridgeline Partners" in body
    assert "Ops team" in body
    assert invoice.label in body
    assert "+12025550999" not in body


def test_profile_panel_reads_in_time_zone_with_its_abbreviation(staff, settings):
    settings.TIME_ZONE = NY
    contact = _quiet_contact()
    AuditEntryFactory(
        model=type(contact),
        object_id=contact.pk,
        trip_id=None,
        profile=f"contact:{contact.pk}",
        changes={"notes": ["", "VIP"]},
        timestamp=datetime(2025, 7, 4, 3, 30, tzinfo=UTC),
    )

    body = _get(staff, "contact", contact.pk).content.decode()

    assert "11:30 PM EDT" in body
    assert "Thu Jul 3, 2025" in body


def _quiet_contact():
    with disable_auditlog():
        return ContactFactory()


def test_vendor_profile_fragment_includes_driver_and_vendor_bill(staff):
    vendor = VendorFactory()
    other = VendorFactory()
    with _portal():
        VendorDriverFactory(vendor=vendor, name="Sam Ortiz")
        AffiliatePayable.objects.create(
            assignment=AssignmentFactory(vendor=vendor), expected_amount=Decimal("350")
        )
        VendorDriverFactory(vendor=other, name="Lee Park")

    body = _get(staff, "vendor", vendor.pk).content.decode()

    assert "Added Sam Ortiz" in body
    assert "Added Vendor bill" in body
    assert "Lee Park" not in body


def test_company_profile_fragment(staff):
    company = CompanyFactory(name="Ridgeline")
    with _portal():
        company.notes = "Net 30 only"
        company.save()

    body = _get(staff, "company", company.pk).content.decode()

    assert "Net 30 only" in body


# --- reading ---


def test_trip_time_rendered_with_zone_abbreviation(staff):
    """Ruling 19: the trip's times take the abbreviation of its pickup date (January, so
    PST), even though the change was made in July (PDT, which the group's clock shows)."""
    trip = _quiet_trip(pickup_timezone=LA, pickup_date=date(2026, 1, 15))
    AuditEntryFactory(
        object_id=trip.pk,
        trip_id=trip.pk,
        changes={"pickup_time": ["19:30:00", "20:00:00"]},
        timestamp=datetime(2025, 7, 10, 22, 12, tzinfo=UTC),
    )

    body = _get(staff, "trip", trip.pk).content.decode()

    assert "7:30 PM PST" in body
    assert "8:00 PM PST" in body
    assert "3:12 PM PDT" in body
    assert "Thu Jul 10, 2025" in body


def test_who_is_the_person_or_the_source(staff):
    trip = _quiet_trip()
    user = UserFactory(first_name="Ana", last_name="Ruiz")
    AuditEntryFactory(
        object_id=trip.pk, trip_id=trip.pk, actor=user, changes={"la_confirmation": ["", "x"]}
    )

    body = _get(staff, "trip", trip.pk).content.decode()

    assert "Ana Ruiz" in body


def test_hide_automatic_keeps_customer_rows(staff):
    trip = _quiet_trip()
    for source in ("cron: charge-due-balances", "webhook: stripe", "customer: quote page"):
        AuditEntryFactory(
            object_id=trip.pk,
            trip_id=trip.pk,
            source=source,
            changes={"la_confirmation": ["", source]},
        )
    base = reverse("audit:trip", args=[trip.pk])

    everything = _get(staff, "trip", trip.pk).content.decode()
    people = _get(staff, "trip", trip.pk, auto="0").content.decode()

    assert "Scheduled job · charge-due-balances" in everything
    assert "Stripe" in everything
    assert "Customer · quote page" in people
    assert "Scheduled job" not in people
    assert "Stripe" not in people
    # The All / People pair reloads the fragment through the drawer.
    assert f"{base}?auto=0" in everything
    assert f'data-url="{base}"' in people


def test_long_text_reads_as_edited_with_a_toggle(staff):
    trip = _quiet_trip()
    AuditEntryFactory(
        model=Assignment,
        object_repr="Vendor assignment",
        trip_id=trip.pk,
        changes={"note": ["", "x" * 120]},
    )

    body = _get(staff, "trip", trip.pk).content.decode()

    assert "Vendor assignment · Note edited" in body
    assert "Show" in body


def test_empty_state_text(staff):
    AuditEntryFactory(timestamp=datetime(2026, 11, 3, 15, 0, tzinfo=UTC))
    trip = _quiet_trip()

    body = _get(staff, "trip", trip.pk).content.decode()

    assert "No changes recorded. History starts Nov 3, 2026" in body


def test_empty_state_before_any_entry_starts_today(staff, settings):
    trip = _quiet_trip()
    assert not AuditEntry.objects.exists()

    resp = _get(staff, "trip", trip.pk)

    assert resp.context["launch_date"] == datetime.now(ZoneInfo(settings.TIME_ZONE)).date()


# --- paging and cost ---


def _groups(trip: Reservation, count: int, *, per_group: int = 2) -> None:
    start = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
    for n in range(count):
        for i in range(per_group):
            AuditEntryFactory(
                object_id=trip.pk,
                trip_id=trip.pk,
                cid=f"c{n}",
                changes={"la_confirmation": [f"{n}.{i}", f"{n}.{i + 1}"]},
                timestamp=start + timedelta(minutes=n, seconds=i),
            )


def test_before_pagination(staff):
    trip = _quiet_trip()
    _groups(trip, 60)

    first = _get(staff, "trip", trip.pk)
    assert len(first.context["groups"]) == 50
    before = first.context["next_before"]
    assert before
    older = f"{reverse('audit:trip', args=[trip.pk])}?before={before}&amp;part=1"
    assert older in first.content.decode()

    second = _get(staff, "trip", trip.pk, before=before, part=1)
    assert len(second.context["groups"]) == 10
    assert second.context["next_before"] is None
    body = second.content.decode()
    assert "Show older" not in body
    assert "No changes recorded" not in body
    # The newest group of page 2 is c9, the oldest of page 1 c10: no overlap, no gap.
    assert {g.lines[0].new for g in second.context["groups"]} == {f"{n}.1" for n in range(10)}


def test_older_page_keeps_people_only_and_skips_the_carried_day_heading(staff):
    trip = _quiet_trip()
    _groups(trip, 60)

    first = _get(staff, "trip", trip.pk, auto="0")
    before = first.context["next_before"]
    assert "auto=0" in first.context["older_url"]

    second = _get(staff, "trip", trip.pk, before=before, part=1, day="2025-03-01")
    # Every group is on Mar 1, which page 1 already headed.
    assert "Sat Mar 1, 2025" not in second.content.decode()


def test_selector_trims_to_whole_groups():
    from apps.audit.selectors import entries_for

    trip = _quiet_trip()
    _groups(trip, 3, per_group=4)

    rows = entries_for("trip", trip.pk, limit_groups=1)

    assert len(rows) == 4
    assert {row.cid for row in rows} == {"c2"}


def test_query_count_flat_as_entries_grow(staff):
    small, large = _quiet_trip(), _quiet_trip()
    _fk_entries(small, 5)
    _fk_entries(large, 50)

    with CaptureQueriesContext(connection) as few:
        assert _get(staff, "trip", small.pk).status_code == 200
    with CaptureQueriesContext(connection) as many:
        assert _get(staff, "trip", large.pk).status_code == 200

    assert len(few) == len(many)


def _fk_entries(trip: Reservation, count: int) -> None:
    """Entries naming three related models (vehicle type, service type, vendor), each
    with its own person, so resolution must be batched to stay flat."""
    vehicles = [VehicleTypeFactory() for _ in range(2)]
    services = [ServiceTypeFactory() for _ in range(2)]
    vendors = [VendorFactory() for _ in range(2)]
    for n in range(count):
        actor = UserFactory()
        match n % 3:
            case 0:
                model, key, pair = Reservation, "vehicle", vehicles
            case 1:
                model, key, pair = Reservation, "service_type", services
            case _:
                model, key, pair = Assignment, "vendor", vendors
        AuditEntryFactory(
            model=model,
            trip_id=trip.pk,
            actor=actor,
            changes={key: [str(pair[0].pk), str(pair[1].pk)]},
        )


# --- the shared drawer ---


def test_drawer_keeps_the_include_title_as_its_default():
    html = render_to_string("components/drawer.html", {"drawer_title": "Assign trip"})

    assert 'data-title="Assign trip"' in html
    assert 'x-text="title"' in html
    assert "$event.detail.title" in html
    assert ">Assign trip</h2>" in html


def test_drawer_default_title_without_an_include_title():
    html = render_to_string("components/drawer.html", {})

    assert 'data-title="Details"' in html


def test_time_field_in_a_route_is_in_the_trip_zone(staff):
    trip = _quiet_trip(pickup_timezone=NY, pickup_date=date(2026, 7, 1))
    AuditEntryFactory(
        object_id=trip.pk,
        trip_id=trip.pk,
        changes={"route.2.scheduled_time": [str(time(9, 0)), str(time(9, 30))]},
    )

    body = _get(staff, "trip", trip.pk).content.decode()

    assert "Stop 2 time" in body
    assert "9:30 AM EDT" in body


def test_history_urls_cannot_break_out_of_their_js_strings():
    """Autoescape doesn't guard a JS string inside an attribute: the browser decodes `&#x27;`
    back to a quote before Alpine reads it. So the URLs live in data-* attributes, where
    autoescape does hold, and no Alpine expression carries them (Ruling 27)."""

    class Attrs(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.attrs: list[tuple[str, str]] = []

        def handle_starttag(self, tag, attrs):
            self.attrs += [(k, v) for k, v in attrs if v]

    evil = "/portal/history/trip/1/?a=');alert(1)//\"><b>"
    html = render_to_string(
        "audit/_history.html",
        {"older_url": evil, "all_url": evil, "people_url": evil, "groups": [], "rows": []},
    )
    parser = Attrs()
    parser.feed(html)

    carrying = [(k, v) for k, v in parser.attrs if "alert" in v]
    assert carrying == [("data-older-url", evil), ("data-url", evil), ("data-url", evil)]
    assert "<b>" not in html


def test_the_sticky_day_heading_sits_flush_under_a_fixed_height_drawer_header():
    """The heading sticks at top-16, so the drawer header must be exactly h-16: a taller
    padded header let a sliver of the list show under the heading."""
    drawer = render_to_string("components/drawer.html", {})
    header = re.search(r'<header class="([^"]+)"', drawer).group(1).split()
    assert "h-16" in header
    assert not {"py-4", "py-3", "py-2"} & set(header)

    groups = render_to_string(
        "audit/_history_groups.html", {"rows": [("Today", Mock(lines=[], who="", clock=""))]}
    )
    heading = re.search(r'<h3 class="([^"]+)"', groups).group(1).split()
    assert {"sticky", "top-16", "bg-surface", "py-3"} <= set(heading)
