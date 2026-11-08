"""How the trail reads in the History drawer (spec §6.2, §6.3; §9 group 7)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.conf import settings

from apps.accounts.factories import UserFactory
from apps.audit.display import (
    Line,
    entry_lines,
    fk_targets,
    format_value,
    group_entries,
    is_automatic,
    money,
    resolve_fks,
    who,
)
from apps.audit.factories import AuditEntryFactory
from apps.audit.models import AuditEntry
from apps.billing.models import InvoiceLine
from apps.contacts.models import Contact, ContactPhone
from apps.dispatch.models import Assignment
from apps.leads.factories import ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments.models import AffiliatePayable, Charge
from apps.planners.models import PlannerRequest
from apps.reservations.models import Reservation, TripExtra
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db

NY = "America/New_York"
LA = "America/Los_Angeles"
JAN = datetime(2026, 1, 15, 3, 30, tzinfo=UTC)  # Jan 14, 10:30 PM in New York
CREATE, UPDATE, DELETE = (
    AuditEntry.Action.CREATE,
    AuditEntry.Action.UPDATE,
    AuditEntry.Action.DELETE,
)


def _lines(changes: dict, *, model=Reservation, zone: str = NY, fk_names=None, **entry) -> list:
    entry.setdefault("timestamp", JAN)
    row = AuditEntryFactory(model=model, changes=changes, **entry)
    return entry_lines(row, zone=zone, fk_names=fk_names or {})


def _one(changes: dict, **kwargs) -> Line:
    (line,) = _lines(changes, **kwargs)
    return line


def _value(model, name: str, raw, **kwargs) -> str:
    kwargs.setdefault("zone", NY)
    kwargs.setdefault("fk_names", {})
    kwargs.setdefault("show_abbr", True)
    return format_value(model, name, raw, **kwargs)


# --- values (§6.3) ---


def test_choice_reads_as_its_label():
    line = _one({"trip_status": ["unassigned", "done"]})
    assert (line.label, line.old, line.new) == ("Status", "Unassigned", "Done")


def test_blank_and_none_read_as_a_dash():
    assert _value(Reservation, "la_confirmation", "") == "—"
    assert _value(Reservation, "la_confirmation", "None") == "—"
    assert _value(Reservation, "la_confirmation", None) == "—"


def test_fk_resolved_and_a_deleted_one_named_by_its_model():
    suv = VehicleTypeFactory(name="Sprinter")
    gone = suv.pk + 1000
    row = AuditEntryFactory(changes={"vehicle": [str(suv.pk), str(gone)]}, timestamp=JAN)
    names = resolve_fks(fk_targets([row]))
    line = entry_lines(row, zone=NY, fk_names=names)[0]
    assert (line.label, line.old, line.new) == (
        "Vehicle",
        "Sprinter",
        f"deleted vehicle type #{gone}",
    )


def test_money_reads_with_dollars_and_commas():
    line = _one({"rate": ["1000.00", "1250"]})
    assert (line.old, line.new) == ("$1,000.00", "$1,250.00")


def test_money_negative_uses_a_minus_sign_before_the_dollar():
    assert money(Decimal("-125")) == "−$125.00"
    assert money("-1250.5") == "−$1,250.50"


def test_a_refund_charge_amount_reads_negative():
    line = _one(
        {"amount": ["100.00", "125.00"]}, model=Charge, object_repr="Refund $100.00", trip_id=None
    )
    assert (line.old, line.new) == ("−$100.00", "−$125.00")


def test_a_deposit_charge_amount_stays_positive():
    line = _one({"amount": ["100.00", "125.00"]}, model=Charge, object_repr="Deposit $100.00")
    assert (line.old, line.new) == ("$100.00", "$125.00")


def test_percent_hours_and_bool():
    assert _value(Reservation, "gratuity_pct", "18.00") == "18%"
    assert _value(Reservation, "cost_ratio_pct", "62.50") == "62.5%"
    assert _value(Reservation, "hours", "2.50") == "2.5 hrs"
    assert _value(Reservation, "overtime_hours", "3") == "3 hrs"
    assert _value(Reservation, "dropoff_estimated", "True") == "Yes"
    assert _value(Reservation, "dropoff_estimated", "False") == "No"


def test_trip_time_in_the_trip_zone_with_its_abbreviation():
    assert _value(Reservation, "pickup_time", "19:30:00", at=JAN) == "7:30 PM EST"
    line = _one({"pickup_time": ["19:30:00", "20:00:00"]})
    assert (line.label, line.old, line.new) == ("Pickup time", "7:30 PM EST", "8:00 PM EST")


def test_a_zone_change_in_the_same_entry_reads_old_times_in_the_old_zone():
    lines = _lines({"pickup_timezone": [LA, NY], "pickup_time": ["16:30:00", "19:30:00"]}, zone=NY)
    by_label = {line.label: line for line in lines}
    assert (by_label["Time zone"].old, by_label["Time zone"].new) == ("PST", "EST")
    assert (by_label["Pickup time"].old, by_label["Pickup time"].new) == (
        "4:30 PM PST",
        "7:30 PM EST",
    )


def test_date_reads_as_weekday_month_day():
    assert _value(Reservation, "pickup_date", "2026-11-14") == "Sat Nov 14"


def test_datetime_on_a_profile_panel_shows_time_zone_with_its_abbreviation():
    zone = settings.TIME_ZONE
    assert zone == NY
    for raw in ("2026-01-15 03:30:00", "2026-01-15 03:30:00.123456", "2026-01-15 03:30:00+00:00"):
        assert _value(Lead, "quote_sent_at", raw, zone=zone) == "Jan 14, 10:30 PM EST"


def test_datetime_day_heading_follows_the_panel_zone_not_utc():
    row = AuditEntryFactory(
        model=ContactPhone,
        object_repr="Mobile 555-0100",
        changes={"texting": ["False", "True"]},
        timestamp=JAN,
        trip_id=None,
        profile="contact:1",
    )
    (group,) = group_entries([row], zone_for=lambda e: settings.TIME_ZONE, fk_names={})
    assert group.day == date(2026, 1, 14)
    assert group.clock == "10:30 PM EST"


# --- lines (§6.2) ---


def test_create_of_a_trip_reads_created_the_trip():
    assert (
        _one({"trip_status": ["None", "unassigned"]}, action=CREATE).sentence == "Created the trip"
    )


def test_create_of_a_child_reads_added_its_label():
    line = _one(
        {"amount": ["None", "450.00"], "kind": ["None", "trip"]},
        model=Charge,
        action=CREATE,
        object_repr="Trip charge $450.00",
    )
    assert line.sentence == "Added Trip charge $450.00"


def test_create_and_delete_of_an_extra_name_it_an_extra():
    assert (
        _one(
            {"amount": ["None", "25"]}, model=TripExtra, action=CREATE, object_repr="Tolls $25.00"
        ).sentence
        == "Added extra Tolls $25.00"
    )
    assert (
        _one(
            {"amount": ["25", "None"]}, model=TripExtra, action=DELETE, object_repr="Tolls $25.00"
        ).sentence
        == "Removed extra Tolls $25.00"
    )


def test_delete_reads_removed_its_label():
    line = _one(
        {"number": ["555", "None"]}, model=ContactPhone, action=DELETE, object_repr="Mobile 555"
    )
    assert line.sentence == "Removed Mobile 555"


def test_invoice_line_create_and_delete():
    names = {("billing.invoice", "7"): "Invoice 1042"}
    created = _one(
        {"invoice": ["None", "7"], "amount": ["None", "450.00"]},
        model=InvoiceLine,
        action=CREATE,
        fk_names=names,
    )
    assert created.sentence == "Invoiced on Invoice 1042 $450.00"
    removed = _one(
        {"invoice": ["7", "None"], "amount": ["450.00", "None"]},
        model=InvoiceLine,
        action=DELETE,
        fk_names=names,
    )
    assert removed.sentence == "Removed from Invoice 1042"


def test_m2m_reads_as_added_and_removed_names():
    add = {"vehicle_types": {"type": "m2m", "operation": "add", "objects": ["Sprinter"]}}
    assert _one(add, model=Vendor).sentence == "Vehicle types: added Sprinter"
    delete = {
        "vehicle_types": {"type": "m2m", "operation": "delete", "objects": ["Sprinter", "Coach"]}
    }
    assert _one(delete, model=Vendor).sentence == "Vehicle types: removed Sprinter, Coach"


def test_route_field_reads_stop_n_and_its_field():
    line = _one({"route.2.address": ["1 Main St, Boston", "Logan Airport (BOS)"]})
    assert (line.label, line.old, line.new) == (
        "Stop 2 address",
        "1 Main St, Boston",
        "Logan Airport (BOS)",
    )


def test_route_time_direction_and_airline():
    lines = _lines(
        {
            "route.1.scheduled_time": ["None", "19:30:00"],
            "route.1.flight_direction": ["arrival", "departure"],
            "route.1.airline": ["None", "UA — United Airlines"],
        }
    )
    by_label = {line.label: (line.old, line.new) for line in lines}
    assert by_label["Stop 1 time"] == ("—", "7:30 PM EST")
    assert by_label["Stop 1 flight direction"] == ("Arriving", "Departing")
    assert by_label["Stop 1 airline"] == ("—", "UA — United Airlines")


def test_a_brand_new_route_reads_route_set():
    line = _one({"route.1": [None, "BOS"], "route.2": [None, "1 Main St"]})
    assert line.sentence == "Route set: BOS → 1 Main St"


def test_a_stop_added_or_removed_on_an_existing_route():
    assert _one({"route.3": [None, "Hotel"]}).sentence == "Added stop 3: Hotel"
    assert _one({"route.3": ["Hotel", None]}).sentence == "Removed stop 3: Hotel"


def test_address_keys_read_slot_and_field_in_a_fixed_order():
    # The JSON column keeps its own key order (MySQL and Postgres both sort keys), so the
    # lines follow the slot and field order, not the stored one.
    lines = _lines(
        {
            "primary_address.line1": ["1 Main St", "2 Main St"],
            "billing_address.postal": ["02110", "02111"],
            "primary_address.landmark_name": ["", "Logan"],
            "primary_address.line2": ["", "Suite 4"],
        },
        model=Contact,
    )
    assert [line.label for line in lines] == [
        "Primary address · Place name",
        "Primary address · Street",
        "Primary address · Street line 2",
        "Billing address · ZIP",
    ]
    assert (lines[0].old, lines[0].new) == ("—", "Logan")


def test_passenger_list_and_headcount_labels():
    lines = _lines(
        {
            "named_passengers": ["Ana Ruiz (lead)", "Ana Ruiz (lead), Ben Ortiz"],
            "passengers": ["1", "2"],
        }
    )
    assert [line.label for line in lines] == ["Passenger count", "Passengers"]
    assert (lines[0].old, lines[0].new) == ("1", "2")


def test_money_rows_name_themselves_before_the_field():
    line = _one(
        {"status": ["pending", "succeeded"]}, model=Charge, object_repr="Trip charge $450.00"
    )
    assert (line.label, line.old, line.new) == (
        "Trip charge $450.00 · Status",
        "Pending",
        "Succeeded",
    )
    bill = _one(
        {"status": ["draft", "approved"]}, model=AffiliatePayable, object_repr="Vendor bill"
    )
    assert bill.label == "Vendor bill · Status"


def test_a_note_over_80_characters_is_long():
    note = "x" * 81
    line = _one({"notes": ["short", note]}, model=Lead)
    assert line.long and (line.label, line.old, line.new) == ("Notes", "short", note)
    assert not _one({"notes": ["short", "x" * 80]}, model=Lead).long


# --- customer, affiliate and planner sentences ---


def test_customer_confirmed_the_trip():
    line = _one({"customer_confirmed_at": ["None", "2026-01-15 03:30:00"]})
    assert line.sentence == "Confirmed the trip"


def test_accepted_terms_names_the_version_and_absorbs_its_line():
    lines = _lines(
        {
            "accepted_terms_at": ["None", "2026-01-15 03:30:00"],
            "accepted_terms_version": ["", "3f9a1c2b"],
        },
        model=Lead,
    )
    assert [line.sentence for line in lines] == ["Accepted the terms (version 3f9a1c2b)"]


def test_fees_acknowledged():
    line = _one({"fees_acknowledged_at": ["", "2026-01-15 03:30:00"]}, model=Lead)
    assert line.sentence == "Acknowledged the cleaning fee and overtime terms"


def test_affiliate_confirmed_the_trip():
    line = _one({"affiliate_confirmed_at": ["None", "2026-01-15 03:30:00"]}, model=Assignment)
    assert line.sentence == "Confirmed the trip"


def test_planner_request_create_reads_its_question():
    line = _one(
        {"about": ["None", "Can we add a second Sprinter?"], "lead": ["None", "3"]},
        model=PlannerRequest,
        action=CREATE,
        object_repr="Planner request: Can we add a second Sprinter?",
    )
    assert line.sentence == 'Planner request: "Can we add a second Sprinter?"'


def test_a_stamp_moving_between_two_times_is_an_ordinary_line():
    line = _one({"customer_confirmed_at": ["2026-01-14 03:30:00", "2026-01-15 03:30:00"]})
    assert not line.sentence
    assert (line.label, line.new) == ("Customer confirmed", "Jan 14, 10:30 PM EST")


# --- who and automatic ---


@pytest.mark.parametrize(
    ("source", "actor_email", "expected"),
    [
        ("webhook: stripe", "", "Stripe"),
        ("webhook: gnet", "", "GNet"),
        ("webhook: podium", "", "Podium"),
        ("webhook: limoanywhere", "", "LimoAnywhere"),
        ("webhook: calendly", "", "Calendly"),
        ("customer: quote page", "", "Customer · quote page"),
        ("customer: trip confirmation", "", "Customer · trip confirmation"),
        ("customer: wedding details", "", "Customer · wedding details"),
        ("affiliate: trip confirmation", "", "Affiliate · trip confirmation"),
        ("planner portal", "ana@example.com", "Planner · ana@example.com"),
        ("cron: charge-due-balances", "", "Scheduled job · charge-due-balances"),
        ("command: shell", "", "Command · shell"),
        ("public site", "", "Public site"),
        ("system", "", "System"),
    ],
)
def test_who_without_an_actor_reads_the_source(source, actor_email, expected):
    assert who(AuditEntryFactory(source=source, actor_email=actor_email)) == expected


def test_who_is_the_actor_full_name_else_email():
    jane = UserFactory(first_name="Jane", last_name="Doe")
    assert who(AuditEntryFactory(actor=jane, actor_email=jane.email)) == "Jane Doe"
    nameless = UserFactory()
    assert who(AuditEntryFactory(actor=nameless, actor_email="x@allprocharter.com")) == (
        "x@allprocharter.com"
    )


def test_who_for_an_import_names_the_file():
    jane = UserFactory(first_name="Jane", last_name="Doe")
    row = AuditEntryFactory(actor=jane, source="import: contacts.xlsx")
    assert who(row) == "Jane Doe via Excel import (contacts.xlsx)"


@pytest.mark.parametrize(
    ("source", "automatic"),
    [
        ("cron: charge-due-balances", True),
        ("webhook: stripe", True),
        ("command: shell", True),
        ("system", True),
        ("portal", False),
        ("customer: quote page", False),
        ("customer: trip confirmation", False),
        ("affiliate: trip confirmation", False),
        ("planner portal", False),
        ("import: contacts.xlsx", False),
    ],
)
def test_is_automatic_keeps_people_including_customers(source, automatic):
    assert is_automatic(AuditEntryFactory(source=source)) is automatic


# --- grouping ---


def test_consecutive_entries_sharing_a_cid_merge_newest_first():
    stamp = lambda minute: datetime(2026, 1, 15, 20, minute, tzinfo=UTC)  # noqa: E731
    newest = AuditEntryFactory(cid="r-a", timestamp=stamp(5), changes={"rate": ["1", "2"]})
    older = AuditEntryFactory(cid="r-a", timestamp=stamp(4), changes={"hours": ["1", "2"]})
    other = AuditEntryFactory(cid="r-b", timestamp=stamp(3), changes={"rate": ["2", "3"]})
    again = AuditEntryFactory(cid="r-a", timestamp=stamp(2), changes={"rate": ["3", "4"]})
    lone1 = AuditEntryFactory(cid=None, timestamp=stamp(1), changes={"rate": ["4", "5"]})
    lone2 = AuditEntryFactory(cid=None, timestamp=stamp(0), changes={"rate": ["5", "6"]})
    groups = group_entries(
        [newest, older, other, again, lone1, lone2], zone_for=lambda e: NY, fk_names={}
    )
    assert [len(g.lines) for g in groups] == [2, 1, 1, 1, 1]
    first = groups[0]
    assert first.when == newest.timestamp
    assert first.zone == NY and first.clock == "3:05 PM EST"
    assert [line.label for line in first.lines] == ["Override hours", "Rate"]


def test_group_who_and_automatic():
    row = AuditEntryFactory(source="webhook: stripe", changes={"rate": ["1", "2"]}, timestamp=JAN)
    (group,) = group_entries([row], zone_for=lambda e: NY, fk_names={})
    assert (group.who, group.automatic) == ("Stripe", True)


def test_order_panel_prefixes_each_trip_line_and_lines_read_in_order():
    trip = AuditEntryFactory(cid="r-a", changes={"rate": ["1", "2"]}, timestamp=JAN)
    order = AuditEntryFactory(
        model=Lead, cid="r-a", trip_id=None, changes={"promo_code": ["", "VIP"]}, timestamp=JAN
    )
    (group,) = group_entries(
        [order, trip],  # newest first, as the selector returns them
        zone_for=lambda e: NY,
        fk_names={},
        prefix_for=lambda e: "Nov 14 · Airport transfer" if e.trip_id else "",
    )
    assert [(line.prefix, line.label) for line in group.lines] == [
        ("Nov 14 · Airport transfer", "Rate"),
        ("", "Promo code"),
    ]
    (line,) = entry_lines(trip, zone=NY, fk_names={}, trip_prefix="Nov 14 · Airport transfer")
    assert line.prefix == "Nov 14 · Airport transfer"


# --- FK resolution ---


def test_resolve_fks_one_query_per_model(django_assert_num_queries):
    suv, sprinter = VehicleTypeFactory(name="SUV"), VehicleTypeFactory(name="Sprinter")
    airport = ServiceTypeFactory()
    jane = UserFactory(first_name="Jane", last_name="Doe")
    rows = [
        AuditEntryFactory(changes={"vehicle": [str(suv.pk), str(sprinter.pk)]}),
        AuditEntryFactory(changes={"vehicle": ["None", str(suv.pk)]}),
        AuditEntryFactory(changes={"service_type": ["None", str(airport.pk)]}),
        AuditEntryFactory(model=Lead, changes={"assigned_agent": ["None", str(jane.pk)]}),
        AuditEntryFactory(changes={"rate": ["1", "2"]}),
    ]
    targets = fk_targets(rows)
    assert targets == {
        "leads.vehicletype": {str(suv.pk), str(sprinter.pk)},
        "leads.servicetype": {str(airport.pk)},
        "accounts.user": {str(jane.pk)},
    }
    with django_assert_num_queries(3):
        names = resolve_fks(targets)
    assert names[("leads.vehicletype", str(sprinter.pk))] == "Sprinter"
    assert names[("accounts.user", str(jane.pk))] == "Jane Doe"
    assert names[("leads.servicetype", str(airport.pk))] == str(airport)


def test_entries_auditlog_actually_wrote_read_cleanly():
    """Real stored shapes, not literal dicts: a trip's create, an editor-style update with a
    time, a zone, money and a vehicle, a stamp, and the delete."""
    from django.contrib.contenttypes.models import ContentType

    from apps.reservations.factories import ReservationFactory

    trip = ReservationFactory(pickup_timezone=NY)
    coach = VehicleTypeFactory(name="Coach")
    trip.pickup_time = datetime(2026, 1, 1, 19, 30).time()
    trip.pickup_date = date(2026, 11, 14)
    trip.rate = Decimal("1250")
    trip.vehicle = coach
    trip.customer_confirmed_at = JAN
    trip.save()
    pk = trip.pk
    trip.delete()
    rows = list(
        AuditEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Reservation), object_id=pk
        ).order_by("-pk")
    )
    groups = group_entries(rows, zone_for=lambda e: NY, fk_names=resolve_fks(fk_targets(rows)))
    lines = [line for group in groups for line in group.lines]
    by_label = {line.label: line for line in lines if line.label}
    assert lines[0].sentence == f"Removed APC-{600_000 + pk}"  # newest first
    assert lines[-1].sentence == "Created the trip"
    assert by_label["Rate"].new == "$1,250.00"
    assert by_label["Vehicle"].new == "Coach"
    assert by_label["Pickup date"].new == "Sat Nov 14"
    assert by_label["Pickup time"].new.endswith(("PM EST", "PM EDT"))
    assert "Confirmed the trip" in [line.sentence for line in lines]
