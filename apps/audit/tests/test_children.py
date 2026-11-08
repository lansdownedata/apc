"""Route, passengers and addresses: logged on their owner, not registered (spec §3.3, §3.4)."""

import pytest
from auditlog.context import disable_auditlog
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AddressFactory, AirlineFactory, AirportFactory
from apps.audit.children import track_address, track_children
from apps.audit.context import audit_context
from apps.audit.models import AuditEntry
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.dispatch import farm_in, farm_in_order
from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import LeadFactory
from apps.public.services import create_lead_from_booking
from apps.reservations import groups
from apps.reservations.drafts import save_reservation_from_draft
from apps.reservations.editor import reservation_draft
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation, Stop
from apps.reservations.passengers import save_passengers
from apps.reservations.routing import reverse_route
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db

UPDATE = AuditEntry.Action.UPDATE


def _entries(model, **filters):
    ct = ContentType.objects.get_for_model(model)
    return AuditEntry.objects.filter(content_type=ct, **filters)


def _trip_updates(trip):
    return _entries(Reservation, trip_id=trip.pk, action=UPDATE).order_by("pk")


def _child_entries(trip, prefix: str = "route"):
    """The trip's UPDATE entries carrying a `route...` (or `named_passengers`) key."""
    return [
        e for e in _trip_updates(trip) if any(key.startswith(prefix) for key in e.changes or {})
    ]


def _payload(**over):
    base = {
        "tripType": "transfer",
        "date": "2026-07-04",
        "time": "15:00",
        "vehicle": "",
        "pax": 3,
        "rate": 185,
        "hours": 1,
        "minHours": 0,
        "stops": [{"address": "Dulles", "note": ""}, {"address": "The Ritz", "note": ""}],
    }
    base.update(over)
    return base


# --- route ----------------------------------------------------------------------------


def test_editor_change_stop2_address_logs_only_that_key():
    lead = LeadFactory()
    res = save_reservation_from_draft(lead, _payload())
    seen = {e.pk for e in _child_entries(res)}

    save_reservation_from_draft(
        lead,
        _payload(stops=[{"address": "Dulles", "note": ""}, {"address": "Four Seasons"}]),
        instance=res,
    )

    (entry,) = [e for e in _child_entries(res) if e.pk not in seen]
    assert entry.changes == {"route.2.address": ["The Ritz", "Four Seasons"]}
    assert entry.trip_id == res.pk and entry.order_id == lead.pk


def test_editor_unchanged_save_logs_no_route_entry():
    lead = LeadFactory()
    res = save_reservation_from_draft(lead, _payload())
    save_passengers(res, [{"name": "Ana Ruiz", "mobile": "+12025550123", "is_lead": True}])
    # The editor's draft pins the estimated drop-off as typed; that first round-trip is a
    # real change to the trip, so the unchanged save is the one after it.
    save_reservation_from_draft(lead, reservation_draft(res), instance=res)
    res.refresh_from_db()
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    save_reservation_from_draft(lead, reservation_draft(res), instance=res)

    assert not AuditEntry.objects.exclude(pk__in=seen).exists()


def test_new_trip_route_set_logs_added_stops():
    res = save_reservation_from_draft(LeadFactory(), _payload())

    (entry,) = _child_entries(res)
    assert entry.changes == {"route.1": [None, "Dulles"], "route.2": [None, "The Ritz"]}


def test_route_fields_are_strings_with_names_and_hh_mm_ss():
    airport = AirportFactory(iata="IAD", name="Washington Dulles International")
    airline = AirlineFactory(iata="UA", name="United Airlines")
    res = TransferReservationFactory()
    seen = {e.pk for e in _child_entries(res)}

    with track_children(res):
        Stop.objects.filter(reservation=res, sequence=0).update(
            airport=airport,
            airline=airline,
            flight_number="123",
            flight_direction="arrival",
            scheduled_time="07:30",
            name="Mr. Adam",
            note="Curbside",
        )
        Stop.objects.filter(reservation=res, sequence=1).delete()

    (entry,) = [e for e in _child_entries(res) if e.pk not in seen]
    assert entry.changes == {
        "route.1.name": ["", "Mr. Adam"],
        "route.1.note": ["", "Curbside"],
        "route.1.scheduled_time": [None, "07:30:00"],
        "route.1.airport": [None, str(airport)],
        "route.1.airline": [None, str(airline)],
        "route.1.flight_number": ["", "123"],
        "route.1.flight_direction": ["", "arrival"],
        "route.2": ["Drop-off", None],
    }


def test_group_apply_logs_per_sibling():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    siblings = [r for r in Reservation.objects.filter(group_key=res.group_key) if r != res]
    seen = set(AuditEntry.objects.values_list("pk", flat=True))
    Stop.objects.filter(reservation=res, sequence=1).update(address="New Drop")

    groups.apply_to_group(res)

    for sibling in siblings:
        (entry,) = [e for e in _child_entries(sibling) if e.pk not in seen]
        assert entry.changes == {"route.2.address": ["Drop-off", "New Drop"]}
    assert not [e for e in _child_entries(res) if e.pk not in seen]


def _gnet_request(payload) -> FarmInRequest:
    AirportFactory(iata="LAX", name="Los Angeles International", timezone="America/Los_Angeles")
    AirlineFactory(iata="UA")
    farm_in.handle_new(payload, transaction_id="tid-audit", delivery_id="d-1")
    return FarmInRequest.objects.select_related("lead", "reservation").get(
        transaction_id="tid-audit"
    )


def test_gnet_apply_update_logs_route_and_passengers():
    payload = sample_payload()
    payload["passengers"] = [{"firstName": "Alex", "lastName": "Smith"}]
    req = _gnet_request(payload)
    res = req.reservation
    (created,) = _child_entries(res)
    assert created.changes["named_passengers"] == ["", "Alex Smith"]
    assert created.changes["route.1"][0] is None

    update = sample_payload()
    update["locations"]["stops"][0]["address"] = "200 N Rodeo Dr"
    update["passengers"] = [
        {"firstName": "Dana", "lastName": "Lee", "phoneNumber": "+12025550123"},
        {"firstName": "Sam", "lastName": "Ray"},
    ]
    farm_in_order.apply_update(res, req.lead, update, summary=farm_in.summarize(update))

    (entry,) = [e for e in _child_entries(res) if e.pk != created.pk]
    assert entry.changes == {
        "route.2.address": ["9876 Wilshire Blvd, Beverly Hills, CA 90210, USA", "200 N Rodeo Dr"],
        "named_passengers": ["Alex Smith", "Dana Lee (lead) •••0123, Sam Ray"],
    }


def test_reverse_logs_two_address_changes():
    res = TransferReservationFactory()
    seen = {e.pk for e in _child_entries(res)}

    reverse_route(res)

    (entry,) = [e for e in _child_entries(res) if e.pk not in seen]
    assert entry.changes == {
        "route.1.address": ["Pickup", "Drop-off"],
        "route.2.address": ["Drop-off", "Pickup"],
    }


def test_public_quote_form_logs_route_set_with_public_source():
    data = {
        "name": "Jane Rider",
        "email": "jane@example.com",
        "passengers": 2,
        "stops": [{"address": "123 Main St"}, {"address": "Reston Town Center"}],
    }
    with audit_context(source="public site"):
        lead = create_lead_from_booking(data)

    (entry,) = _child_entries(lead.reservations.get())
    assert entry.changes == {
        "route.1": [None, "123 Main St"],
        "route.2": [None, "Reston Town Center"],
    }
    assert entry.source == "public site"


def test_exception_inside_block_writes_nothing():
    res = TransferReservationFactory()
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    with pytest.raises(RuntimeError), track_children(res, passengers=True):
        Stop.objects.filter(reservation=res, sequence=1).update(address="Elsewhere")
        raise RuntimeError("boom")

    assert not AuditEntry.objects.exclude(pk__in=seen).exists()


# --- passengers -----------------------------------------------------------------------


def test_save_passengers_logs_names_with_lead_marker():
    res = TransferReservationFactory()

    save_passengers(
        res,
        [
            {"name": "Ana Ruiz", "mobile": "+12025550123", "is_lead": True},
            {"name": "Ben Ortiz", "mobile": "+12025550124"},
            {"name": "", "mobile": "+12025551234"},
        ],
    )

    (entry,) = _child_entries(res, "named_passengers")
    assert entry.changes == {
        "named_passengers": ["", "Ana Ruiz (lead) •••0123, Ben Ortiz •••0124, •••1234"]
    }


def test_save_passengers_same_list_logs_nothing():
    res = TransferReservationFactory()
    rows = [
        {"name": "Ana Ruiz", "mobile": "+12025550123", "is_lead": True},
        {"name": "Ben Ortiz"},
    ]
    save_passengers(res, rows)
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    save_passengers(res, rows)

    assert not AuditEntry.objects.exclude(pk__in=seen).exists()


@pytest.mark.parametrize(
    "after,expected",
    [
        (
            [("Ben Ortiz", False), ("Ana Ruiz", True)],
            "Ben Ortiz •••0124, Ana Ruiz (lead) •••0123",
        ),
        (
            [("Ana Ruiz", False), ("Ben Ortiz", True)],
            "Ana Ruiz •••0123, Ben Ortiz (lead) •••0124",
        ),
    ],
    ids=["order", "lead-marker"],
)
def test_save_passengers_order_or_lead_alone_logs(after, expected):
    res = TransferReservationFactory()
    mobiles = {"Ana Ruiz": "+12025550123", "Ben Ortiz": "+12025550124"}
    save_passengers(
        res,
        [
            {"name": "Ana Ruiz", "mobile": mobiles["Ana Ruiz"], "is_lead": True},
            {"name": "Ben Ortiz", "mobile": mobiles["Ben Ortiz"]},
        ],
    )
    seen = {e.pk for e in _child_entries(res, "named_passengers")}

    save_passengers(res, [{"name": n, "mobile": mobiles[n], "is_lead": lead} for n, lead in after])

    (entry,) = [e for e in _child_entries(res, "named_passengers") if e.pk not in seen]
    assert entry.changes == {
        "named_passengers": ["Ana Ruiz (lead) •••0123, Ben Ortiz •••0124", expected]
    }


def test_save_passengers_lead_mobile_alone_logs():
    """Messages go to the lead's number, so changing only that number is a change."""
    res = TransferReservationFactory()
    save_passengers(res, [{"name": "Ana Ruiz", "mobile": "+12025550123", "is_lead": True}])
    seen = {e.pk for e in _child_entries(res, "named_passengers")}

    save_passengers(res, [{"name": "Ana Ruiz", "mobile": "+12025559876", "is_lead": True}])

    (entry,) = [e for e in _child_entries(res, "named_passengers") if e.pk not in seen]
    assert entry.changes == {
        "named_passengers": ["Ana Ruiz (lead) •••0123", "Ana Ruiz (lead) •••9876"]
    }


def test_headcount_and_names_in_one_save_log_two_distinct_keys():
    lead = LeadFactory()
    res = save_reservation_from_draft(lead, _payload())
    save_reservation_from_draft(lead, reservation_draft(res), instance=res)
    res.refresh_from_db()
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    draft = reservation_draft(res)
    draft["pax"] = 5
    draft["passengerList"] = [{"name": "Ana Ruiz", "mobile": "", "lead": False}]
    save_reservation_from_draft(lead, draft, instance=res)

    changes = [e.changes for e in _trip_updates(res).exclude(pk__in=seen)]
    headcount = [c["passengers"] for c in changes if "passengers" in c]
    names = [c["named_passengers"] for c in changes if "named_passengers" in c]
    assert headcount == [["3", "5"]]
    assert names == [["", "Ana Ruiz"]]


# --- addresses ------------------------------------------------------------------------


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _address_entries(model, owner, key_prefix: str):
    kind = model._meta.model_name
    return [
        e
        for e in _entries(model, profile=f"{kind}:{owner.pk}", action=UPDATE).order_by("pk")
        if any(key.startswith(key_prefix) for key in e.changes or {})
    ]


def test_contact_address_update_logs_slot_fields(staff):
    contact = ContactFactory(primary_address=AddressFactory(line1="14 Beacon St", city="Boston"))

    resp = staff.post(
        reverse("contact_address_update", args=[contact.pk, "primary"]),
        {"line1": "14 Beacon St", "city": "Cambridge", "latitude": "42.37"},
    )

    assert resp.status_code == 200
    (entry,) = _address_entries(Contact, contact, "primary_address.")
    assert entry.changes == {"primary_address.city": ["Boston", "Cambridge"]}
    assert entry.profile == f"contact:{contact.pk}"
    assert entry.source == "portal"


def test_first_address_old_side_blank(staff):
    """Blank before, except fields with a model default: no country was posted, so the
    country the new Address defaults to is no change."""
    contact = ContactFactory()
    assert contact.billing_address_id is None

    staff.post(
        reverse("contact_address_update", args=[contact.pk, "billing"]),
        {"line1": "14 Beacon St", "city": "Boston"},
    )

    (entry,) = _address_entries(Contact, contact, "billing_address.")
    assert entry.changes == {
        "billing_address.line1": ["", "14 Beacon St"],
        "billing_address.city": ["", "Boston"],
    }
    # Pointing the contact at its new Address is auditlog's own entry on the save, not ours.
    contact.refresh_from_db()
    fk = [
        e
        for e in _entries(Contact, object_id=contact.pk, action=UPDATE)
        if "billing_address" in e.changes
    ]
    assert [e.changes["billing_address"][1] for e in fk] == [str(contact.billing_address_id)]


def test_vendor_address_update_logs(staff):
    vendor = VendorFactory(address=AddressFactory(city="Reston", state="VA"))

    staff.post(reverse("vendor_address_update", args=[vendor.pk]), {"city": "Herndon"})

    (entry,) = _address_entries(Vendor, vendor, "address.")
    assert entry.changes == {"address.city": ["Reston", "Herndon"]}
    assert entry.profile == f"vendor:{vendor.pk}"


def test_unchanged_address_post_logs_nothing(staff):
    contact = ContactFactory(primary_address=AddressFactory(line1="14 Beacon St", city="Boston"))
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    staff.post(
        reverse("contact_address_update", args=[contact.pk, "primary"]),
        {"line1": "14 Beacon St", "city": "Boston", "place_id": "abc", "latitude": "42.35"},
    )

    assert not AuditEntry.objects.exclude(pk__in=seen).exists()


def test_first_address_posting_a_country_logs_it_from_the_default(staff):
    contact = ContactFactory()

    staff.post(
        reverse("contact_address_update", args=[contact.pk, "primary"]),
        {"city": "Toronto", "country": "Canada"},
    )

    (entry,) = _address_entries(Contact, contact, "primary_address.")
    assert entry.changes == {
        "primary_address.city": ["", "Toronto"],
        "primary_address.country": ["United States", "Canada"],
    }


# --- auditlog switched off --------------------------------------------------------------


def test_trackers_write_nothing_while_auditlog_is_disabled():
    res = TransferReservationFactory()
    contact = ContactFactory(primary_address=AddressFactory(city="Boston"))
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    with disable_auditlog():
        with track_children(res, passengers=True):
            Stop.objects.filter(reservation=res, sequence=1).update(address="Elsewhere")
            save_passengers(res, [{"name": "Ana Ruiz"}])
        with track_address(contact, slot="primary_address"):
            contact.primary_address.city = "Cambridge"
            contact.primary_address.save()

    assert not AuditEntry.objects.exclude(pk__in=seen).exists()


def test_reverse_with_propagate_logs_once_on_source_and_each_sibling():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    members = list(Reservation.objects.filter(group_key=res.group_key))
    assert len(members) == 3
    seen = set(AuditEntry.objects.values_list("pk", flat=True))

    reverse_route(res, propagate=True)

    for trip in members:
        (entry,) = [e for e in _child_entries(trip) if e.pk not in seen]
        assert entry.changes == {
            "route.1.address": ["Pickup", "Drop-off"],
            "route.2.address": ["Drop-off", "Pickup"],
        }
