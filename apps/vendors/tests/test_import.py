"""Vendor import (APC-141): columns (spec §6.1), matching (§6.2), vehicle types (§6.3), address
(§5.4), side effects (§6.4)."""

from decimal import Decimal
from unittest import mock

import pytest
import tablib

from apps.addresses.factories import AddressFactory
from apps.addresses.models import Address
from apps.billing.models import SyncState
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor
from apps.vendors.resources import VENDOR_COLUMNS, VendorResource

pytestmark = pytest.mark.django_db

VENDOR_HEADERS = [c.name for c in VENDOR_COLUMNS]
SYNC_VENDOR = "apps.tasks.vendor_tasks.sync_vendor"


def run_import(rows, *, headers=VENDOR_HEADERS, dry_run=False, resource=None):
    """Import `rows` (dicts keyed by column name; missing cells blank) as Excel rows 2.. .

    The outcome dicts (spec §3.4) are attached to the result as `result.outcomes`.
    """
    dataset = tablib.Dataset(headers=[*headers, "__row__"])
    for excel_row, row in enumerate(rows, start=2):
        dataset.append([row.get(h, "") for h in headers] + [excel_row])
    resource = resource or VendorResource()
    result = resource.import_data(
        dataset, dry_run=dry_run, use_transactions=True, rollback_on_validation_errors=False
    )
    assert not result.has_errors(), [e.error for r in result.row_errors() for e in r[1]]
    result.outcomes = [
        resource.outcome(row_result, dict(zip(dataset.headers, values, strict=True)))
        for row_result, values in zip(result.rows, dataset, strict=True)
    ]
    return result, resource


def _types(vendor: Vendor) -> set[str]:
    return set(vendor.vehicle_types.values_list("name", flat=True))


@pytest.fixture(autouse=True)
def _no_sync():
    """`sync_vendor` is a side effect asserted where it matters; elsewhere it is a no-op."""
    with mock.patch(SYNC_VENDOR) as sync:
        yield sync


# --- Create and update (spec §6.1) ----------------------------------------------------------


def test_creates_vendor_active_by_default():
    result, _ = run_import(
        [{"Name": "Capital Cars", "Email": "ops@acme.test", "Phone": "(202) 555-0143"}]
    )
    vendor = Vendor.objects.get()
    assert (vendor.name, vendor.email, vendor.phone) == (
        "Capital Cars",
        "ops@acme.test",
        "+12025550143",
    )
    assert vendor.status == Vendor.Status.ACTIVE
    out = result.outcomes[0]
    assert (out["outcome"], out["row"], out["label"], out["key"]) == (
        "new",
        2,
        "Capital Cars",
        "ops@acme.test",
    )
    assert out["object_id"] == vendor.pk
    assert out["changes"] == [
        ["Name", "", "Capital Cars"],
        ["Email", "", "ops@acme.test"],
        ["Phone", "", "(202) 555-0143"],
    ]


def test_updates_by_email():
    vendor = VendorFactory(name="Capital Cars", email="Ops@acme.test", notes="")
    result, _ = run_import([{"Email": "ops@ACME.test", "Notes": "Prefers DCA runs"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["object_id"] == vendor.pk
    assert out["label"] == "Capital Cars"
    # Matched case-insensitively: the stored casing is not a change.
    assert out["changes"] == [["Notes", "", "Prefers DCA runs"]]
    vendor.refresh_from_db()
    assert (vendor.notes, vendor.email) == ("Prefers DCA runs", "Ops@acme.test")
    assert Vendor.objects.count() == 1


def test_email_matching_two_vendors_is_an_error_naming_both():
    a = VendorFactory(name="Capital Cars", email="ops@acme.test")
    b = VendorFactory(name="Capital Cars LLC", email="OPS@acme.test")
    result, _ = run_import([{"Email": "ops@acme.test", "Notes": "x"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == (
        f"Email matches 2 vendors: Capital Cars (#{a.pk}), Capital Cars LLC (#{b.pk}). "
        "Fix the duplicates first."
    )
    assert Vendor.objects.filter(notes="x").count() == 0


def test_phone_fallback_and_multi_match_error():
    texter = VendorFactory(name="Texter", email="t@acme.test", phone="+12025550143", notes="")
    legacy = VendorFactory(name="Legacy", email="", phone="555-0199 ext 4", notes="")
    a = VendorFactory(name="Twin A", email="a@acme.test", phone="+16175550101")
    b = VendorFactory(name="Twin B", email="", phone="(617) 555-0101")
    c = VendorFactory(name="Blank C", email="", phone="+16175550102")
    d = VendorFactory(name="Blank D", email="", phone="617-555-0102")
    result, _ = run_import(
        [
            {"Phone": "(202) 555-0143", "Notes": "found by E.164"},
            {"Phone": "555-0199 ext 4", "Notes": "found as typed"},
            {"Phone": "(617) 555-0101", "Notes": "two vendors"},
            {"Email": "new@acme.test", "Phone": "617-555-0102", "Notes": "two email-less"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["updated", "updated", "error", "error"]
    assert [o["object_id"] for o in result.outcomes[:2]] == [texter.pk, legacy.pk]
    assert result.outcomes[0]["key"] == "(202) 555-0143"
    assert result.outcomes[2]["reason"] == (
        f"Phone matches 2 vendors: Twin A (#{a.pk}), Twin B (#{b.pk}). Fix the duplicates first."
    )
    assert result.outcomes[3]["reason"] == (
        f"Phone matches 2 vendors: Blank C (#{c.pk}), Blank D (#{d.pk}). Fix the duplicates first."
    )
    texter.refresh_from_db()
    legacy.refresh_from_db()
    assert (texter.notes, texter.phone) == ("found by E.164", "+12025550143")
    assert (legacy.notes, legacy.phone) == ("found as typed", "555-0199 ext 4")
    assert Vendor.objects.count() == 6


def test_new_email_fills_in_email_less_vendor():
    vendor = VendorFactory(name="Capital Cars", email="", phone="+12025550143")
    result, _ = run_import([{"Email": "ops@acme.test", "Phone": "(202) 555-0143"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["object_id"] == vendor.pk
    assert out["changes"] == [["Email", "", "ops@acme.test"]]
    vendor.refresh_from_db()
    assert vendor.email == "ops@acme.test"
    assert Vendor.objects.count() == 1


def test_phone_on_vendor_with_other_email_creates_new_and_warns():
    pat = VendorFactory(name="Pat Cars", email="pat@other.test", phone="+12025550143")
    result, _ = run_import(
        [{"Name": "Capital Cars", "Email": "ops@acme.test", "Phone": "(202) 555-0143"}]
    )
    out = result.outcomes[0]
    assert out["outcome"] == "new"
    assert out["key"] == "ops@acme.test"
    assert out["warnings"] == [
        "Phone is also on Pat Cars (pat@other.test) — this creates a separate vendor"
    ]
    assert out["object_id"] == Vendor.objects.get(email="ops@acme.test").pk
    pat.refresh_from_db()
    assert pat.email == "pat@other.test"


def test_no_key_is_an_error():
    result, _ = run_import([{"Name": "Capital Cars", "Notes": "hello"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Needs an email or a phone to match or create a vendor."
    assert not Vendor.objects.exists()


def test_duplicate_key_in_file():
    result, _ = run_import(
        [
            {"Name": "First", "Email": "ops@acme.test"},
            {"Name": "Second", "Email": "OPS@acme.test"},
            {"Name": "Third", "Phone": "(202) 555-0143"},
            {"Name": "Fourth", "Phone": "+12025550143"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["new", "error", "new", "error"]
    assert result.outcomes[1]["reason"] == "Same email as row 2"
    assert result.outcomes[3]["reason"] == "Same phone as row 4"


def test_name_required_on_new_row():
    result, _ = run_import([{"Email": "ops@acme.test", "Notes": "no name"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Name is required for a new vendor."
    assert not Vendor.objects.exists()


def test_blank_never_clears():
    sedan = VehicleTypeFactory(name="Sedan")
    address = AddressFactory(line1="1 Main St", city="Boston")
    vendor = VendorFactory(
        name="Capital Cars",
        contact_name="Pat",
        email="ops@acme.test",
        phone="+12025550143",
        status=Vendor.Status.INACTIVE,
        service_area="DC",
        website="https://capital.acme.test",
        usdot_number="123",
        notes="VIP",
        address=address,
    )
    vendor.vehicle_types.set([sedan])
    result, _ = run_import([{"Email": "ops@acme.test", "Name": "  "}])
    assert result.outcomes[0]["outcome"] == "unchanged"
    assert result.outcomes[0]["changes"] == []
    vendor.refresh_from_db()
    assert (
        vendor.name,
        vendor.contact_name,
        vendor.phone,
        vendor.status,
        vendor.service_area,
        vendor.website,
        vendor.usdot_number,
        vendor.notes,
        vendor.address_id,
    ) == (
        "Capital Cars",
        "Pat",
        "+12025550143",
        "inactive",
        "DC",
        "https://capital.acme.test",
        "123",
        "VIP",
        address.pk,
    )
    assert _types(vendor) == {"Sedan"}


def test_dry_run_object_id_is_none_for_a_record_created_earlier_in_the_run():
    rows = [
        {"Name": "Capital Cars", "Email": "ops@acme.test", "Phone": "(617) 555-0101"},
        {"Phone": "(617) 555-0101", "Notes": "found by the phone row 2 put on file"},
    ]
    result, _ = run_import(rows, dry_run=True)
    assert [o["outcome"] for o in result.outcomes] == ["new", "updated"]
    assert [o["object_id"] for o in result.outcomes] == [None, None]
    assert not Vendor.objects.exists()

    result, _ = run_import(rows)
    vendor = Vendor.objects.get()
    assert [o["object_id"] for o in result.outcomes] == [vendor.pk, vendor.pk]


# --- Phone (spec §6.1) ----------------------------------------------------------------------


def test_phone_stored_e164_or_as_typed():
    vendor = VendorFactory(email="ops@acme.test", phone="+12025550143")
    result, _ = run_import(
        [
            {"Email": "ops@acme.test", "Phone": "617-555-0101"},
            {"Name": "Odd", "Email": "odd@acme.test", "Phone": "ask for Pat"},
        ]
    )
    assert result.outcomes[0]["changes"] == [["Phone", "(202) 555-0143", "(617) 555-0101"]]
    vendor.refresh_from_db()
    assert vendor.phone == "+16175550101"
    assert Vendor.objects.get(email="odd@acme.test").phone == "ask for Pat"


@pytest.mark.parametrize(
    ("stored", "typed"),
    [
        ("+12025550143", "(202) 555-0143"),
        ("+12025550143", "202.555.0143"),
        ("555-0199 ext 4", "555-0199 ext 4"),
        ("(202) 555-0143", "(202) 555-0143"),
    ],
)
def test_same_phone_is_unchanged(stored, typed):
    VendorFactory(email="ops@acme.test", phone=stored)
    result, _ = run_import([{"Email": "ops@acme.test", "Phone": typed}])
    assert result.outcomes[0]["outcome"] == "unchanged"
    assert Vendor.objects.get().phone == stored


# --- Status (spec §6.1) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("Archived", "inactive"),
        ("archived", "inactive"),
        ("INACTIVE", "inactive"),
        ("Active", "active"),
    ],
)
def test_status_archived_means_inactive(typed, stored):
    vendor = VendorFactory(email="ops@acme.test", status=Vendor.Status.ACTIVE)
    result, _ = run_import([{"Email": "ops@acme.test", "Status": typed}])
    vendor.refresh_from_db()
    assert vendor.status == stored
    if stored == "inactive":
        assert result.outcomes[0]["changes"] == [["Status", "Active", "Inactive"]]
    else:
        assert result.outcomes[0]["outcome"] == "unchanged"


def test_bad_status_is_an_error():
    vendor = VendorFactory(email="ops@acme.test", notes="")
    result, _ = run_import([{"Email": "ops@acme.test", "Status": "Paused", "Notes": "x"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Status must be Active or Inactive"
    vendor.refresh_from_db()
    assert vendor.notes == ""


def test_invalid_website_is_an_error():
    result, _ = run_import([{"Name": "Capital Cars", "Email": "ops@acme.test", "Website": "nope"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Website: Enter a valid URL."
    assert not Vendor.objects.exists()


# --- Vehicle types (spec §6.3) --------------------------------------------------------------


def test_vehicle_types_replace_case_insensitive_inactive_accepted():
    sedan = VehicleTypeFactory(name="Sedan")
    suv = VehicleTypeFactory(name="SUV")
    VehicleTypeFactory(name="Sprinter", active=False)
    vendor = VendorFactory(email="ops@acme.test")
    vendor.vehicle_types.set([sedan, suv])
    result, _ = run_import([{"Email": "ops@acme.test", "Vehicle types": "sedan,  Sprinter"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["changes"] == [["Vehicle types", "Sedan, SUV", "Sedan, Sprinter"]]
    assert _types(vendor) == {"Sedan", "Sprinter"}


def test_vehicle_types_same_set_is_unchanged():
    vendor = VendorFactory(email="ops@acme.test")
    vendor.vehicle_types.set([VehicleTypeFactory(name="Sedan"), VehicleTypeFactory(name="SUV")])
    result, _ = run_import([{"Email": "ops@acme.test", "Vehicle types": "suv, SEDAN"}])
    assert result.outcomes[0]["outcome"] == "unchanged"


def test_vehicle_types_on_new_vendor():
    VehicleTypeFactory(name="Sedan")
    VehicleTypeFactory(name="SUV")
    result, _ = run_import(
        [{"Name": "Capital Cars", "Email": "ops@acme.test", "Vehicle types": "suv, sedan"}]
    )
    assert ["Vehicle types", "", "Sedan, SUV"] in result.outcomes[0]["changes"]
    assert _types(Vendor.objects.get()) == {"Sedan", "SUV"}


def test_unknown_vehicle_type_is_an_error_and_nothing_created():
    sedan = VehicleTypeFactory(name="Sedan")
    vendor = VendorFactory(email="ops@acme.test", notes="")
    vendor.vehicle_types.set([sedan])
    before = VehicleType.objects.count()
    result, _ = run_import(
        [
            {
                "Email": "ops@acme.test",
                "Vehicle types": "Sedan, Party bus XL, Hovercraft",
                "Notes": "x",
            },
            {"Name": "New", "Email": "new@acme.test", "Vehicle types": "Party bus XL"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["error", "error"]
    assert result.outcomes[0]["reason"] == (
        "Vehicle type 'Party bus XL' doesn't exist; Vehicle type 'Hovercraft' doesn't exist"
    )
    assert result.outcomes[1]["reason"] == "Vehicle type 'Party bus XL' doesn't exist"
    assert VehicleType.objects.count() == before
    vendor.refresh_from_db()
    assert vendor.notes == ""
    assert _types(vendor) == {"Sedan"}
    assert Vendor.objects.count() == 1


def test_blank_vehicle_types_unchanged():
    vendor = VendorFactory(email="ops@acme.test", notes="")
    vendor.vehicle_types.set([VehicleTypeFactory(name="Sedan")])
    result, _ = run_import([{"Email": "ops@acme.test", "Vehicle types": " ", "Notes": "x"}])
    assert result.outcomes[0]["changes"] == [["Notes", "", "x"]]
    assert _types(vendor) == {"Sedan"}


def test_vehicle_types_loaded_once_per_import():
    for name in ("Sedan", "SUV", "Sprinter"):
        VehicleTypeFactory(name=name)
    rows = [
        {"Name": f"V{i}", "Email": f"v{i}@acme.test", "Vehicle types": "Sedan"} for i in range(3)
    ]
    with mock.patch.object(VehicleType.objects, "all", wraps=VehicleType.objects.all) as all_types:
        result, _ = run_import(rows)
    assert [o["outcome"] for o in result.outcomes] == ["new"] * 3
    assert all_types.call_count == 1


# --- Side effects (spec §6.4) ---------------------------------------------------------------


def test_qbo_not_synced_only_for_name_email_phone():
    vendor = VendorFactory(
        name="Capital Cars",
        email="ops@acme.test",
        phone="+12025550143",
        qbo_sync_state=SyncState.SYNCED,
    )
    run_import([{"Email": "ops@acme.test", "Notes": "x", "Status": "Inactive"}])
    vendor.refresh_from_db()
    assert vendor.qbo_sync_state == SyncState.SYNCED

    run_import([{"Email": "ops@acme.test", "Phone": "(617) 555-0101"}], dry_run=True)
    vendor.refresh_from_db()
    assert vendor.qbo_sync_state == SyncState.SYNCED

    run_import([{"Email": "ops@acme.test", "Phone": "(617) 555-0101"}])
    vendor.refresh_from_db()
    assert (vendor.phone, vendor.qbo_sync_state) == ("+16175550101", SyncState.NOT_SYNCED)


def test_sync_vendor_on_status_change(_no_sync):
    vendor = VendorFactory(email="ops@acme.test", status=Vendor.Status.ACTIVE)
    run_import([{"Email": "ops@acme.test", "Status": "Inactive"}], dry_run=True)
    _no_sync.assert_not_called()
    run_import([{"Email": "ops@acme.test", "Notes": "notes only"}])
    _no_sync.assert_not_called()
    run_import([{"Email": "ops@acme.test", "Status": "Inactive"}])
    _no_sync.assert_called_once()
    assert _no_sync.call_args.args[0].pk == vendor.pk


def test_sync_vendor_on_new_vendor(_no_sync):
    run_import([{"Name": "Capital Cars", "Email": "ops@acme.test"}], dry_run=True)
    _no_sync.assert_not_called()
    run_import([{"Name": "Capital Cars", "Email": "ops@acme.test"}])
    _no_sync.assert_called_once()
    assert _no_sync.call_args.args[0].pk == Vendor.objects.get().pk


def test_gnet_column_is_ignored():
    headers = [*VENDOR_HEADERS, "GNet griddID"]
    run_import(
        [{"Name": "Capital Cars", "Email": "ops@acme.test", "GNet griddID": "GRID-1"}],
        headers=headers,
    )
    vendor = Vendor.objects.get()
    assert vendor.gnet_grid_id == ""
    result, _ = run_import(
        [{"Email": "ops@acme.test", "GNet griddID": "GRID-2", "Notes": "x"}], headers=headers
    )
    assert result.outcomes[0]["changes"] == [["Notes", "", "x"]]
    vendor.refresh_from_db()
    assert vendor.gnet_grid_id == ""


def _counts() -> tuple[int, ...]:
    return (
        Vendor.objects.count(),
        Address.objects.count(),
        VehicleType.objects.count(),
        Vendor.vehicle_types.through.objects.count(),
    )


def test_vendor_preview_writes_nothing():
    sedan = VehicleTypeFactory(name="Sedan")
    VehicleTypeFactory(name="SUV")
    vendor = VendorFactory(
        name="Capital Cars",
        email="ops@acme.test",
        phone="+12025550143",
        status=Vendor.Status.ACTIVE,
        notes="",
        qbo_sync_state=SyncState.SYNCED,
    )
    vendor.vehicle_types.set([sedan])
    before = _counts()
    result, _ = run_import(
        [
            {
                "Name": "New Cars",
                "Email": "new@acme.test",
                "Vehicle types": "Sedan, SUV",
                "Street": "1 Main St",
                "City": "Boston",
            },
            {
                "Email": "ops@acme.test",
                "Name": "Capital Cars LLC",
                "Phone": "(617) 555-0101",
                "Status": "Inactive",
                "Vehicle types": "SUV",
                "Street": "2 Elm St",
                "Notes": "VIP",
            },
        ],
        dry_run=True,
    )
    assert [o["outcome"] for o in result.outcomes] == ["new", "updated"]
    assert _counts() == before
    vendor.refresh_from_db()
    assert (
        vendor.name,
        vendor.phone,
        vendor.status,
        vendor.address,
        vendor.notes,
        vendor.qbo_sync_state,
    ) == ("Capital Cars", "+12025550143", "active", None, "", SyncState.SYNCED)
    assert _types(vendor) == {"Sedan"}


# --- Address (spec §5.4) --------------------------------------------------------------------


def test_address_on_vendor():
    result, _ = run_import(
        [{"Name": "Capital Cars", "Email": "ops@acme.test", "Street": "1 Main St", "ZIP": "02134"}]
    )
    assert ["ZIP", "", "02134"] in result.outcomes[0]["changes"]
    address = Vendor.objects.get().address
    assert (address.line1, address.postal, address.city) == ("1 Main St", "02134", "")

    address.latitude = Decimal("42.35")
    address.save()
    result, _ = run_import([{"Email": "ops@acme.test", "Street": "2 New St", "ZIP": "02134"}])
    assert result.outcomes[0]["changes"] == [["Street", "1 Main St", "2 New St"]]
    address.refresh_from_db()
    assert (address.line1, address.latitude) == ("2 New St", None)
    assert Vendor.objects.get().address_id == address.pk


def test_address_cell_too_long_is_an_error():
    result, _ = run_import([{"Name": "Capital Cars", "Email": "ops@acme.test", "ZIP": "1" * 21}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "ZIP: Ensure this value has at most 20 characters (it has 21)."
    assert not Address.objects.exists()
    assert not Vendor.objects.exists()


# --- Export and round trip (spec §4.2, §6.1, §11) -------------------------------------------


def _export() -> tablib.Dataset:
    queryset = VendorResource.export_queryset(Vendor.objects.order_by("name"))
    return VendorResource().export(queryset=queryset)


def test_export_columns():
    sedan = VehicleTypeFactory(name="sedan")
    suv = VehicleTypeFactory(name="SUV")
    vendor = VendorFactory(
        name="Capital Cars",
        contact_name="Pat",
        email="ops@acme.test",
        phone="+12025550143",
        status=Vendor.Status.INACTIVE,
        service_area="DC metro",
        website="https://capital.acme.test",
        usdot_number="0123",
        notes="VIP",
        gnet_grid_id="GRID-1",
        address=AddressFactory(
            line1="1 Main St", line2="Suite 2", city="Boston", state="MA", postal="02134"
        ),
    )
    vendor.vehicle_types.set([suv, sedan])
    VendorFactory(name="Bare", contact_name="", email="", phone="555-0199 ext 4", service_area="")

    dataset = _export()
    assert dataset.headers == VENDOR_HEADERS
    assert dataset[0] == ("Bare", "", "", "555-0199 ext 4", "Active", *[""] * 9, "")
    assert dataset[1] == (
        "Capital Cars",
        "Pat",
        "ops@acme.test",
        "(202) 555-0143",
        "Inactive",
        "sedan, SUV",
        "DC metro",
        "https://capital.acme.test",
        "0123",
        "1 Main St",
        "Suite 2",
        "Boston",
        "MA",
        "02134",
        "VIP",
    )


def test_export_query_count_is_fixed(django_assert_num_queries):
    types = [VehicleTypeFactory(name=n) for n in ("Sedan", "SUV")]
    for i in range(5):
        vendor = VendorFactory(address=AddressFactory(line1=f"{i} Main St"))
        vendor.vehicle_types.set(types)
    # The library counts the rows, then vendors with their address, then the vehicle types.
    with django_assert_num_queries(3):
        dataset = _export()
    assert len(dataset) == 5


def test_round_trip_is_all_unchanged():
    sedan = VehicleTypeFactory(name="Sedan")
    VendorFactory(
        name="Capital Cars",
        email="Ops@acme.test",
        phone="+12025550143",
        website="https://capital.acme.test",
        usdot_number="0123",
        address=AddressFactory(line1="1 Main St", postal="02134"),
    ).vehicle_types.set([sedan])
    VendorFactory(name="Legacy", email="", phone="(202) 555-0199", status=Vendor.Status.INACTIVE)
    VendorFactory(name="Odd", email="", phone="555-0100 ext 4")

    dataset = _export()
    rows = [dict(zip(dataset.headers, values, strict=True)) for values in dataset]
    result, _ = run_import(rows)
    assert [o["outcome"] for o in result.outcomes] == ["unchanged"] * 3
