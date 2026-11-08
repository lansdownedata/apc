"""Contact import (APC-141): matching (spec §5.2), columns (§5.1), phones (§5.3), address
(§5.4), side effects (§5.5)."""

from decimal import Decimal
from unittest import mock

import pytest
import tablib
from django.core import mail
from django.core.exceptions import ValidationError
from import_export.instance_loaders import BaseInstanceLoader
from import_export.resources import ModelResource
from import_export.results import RowResult

from apps.addresses.factories import AddressFactory
from apps.addresses.models import Address
from apps.billing.factories import CustomerLinkFactory
from apps.billing.models import CustomerLink, SyncState
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Company, Contact, ContactPhone
from apps.contacts.resources import CONTACT_COLUMNS, ContactResource
from apps.core.choices import Channel
from apps.imports.resources import PortalResource

pytestmark = pytest.mark.django_db

CONTACT_HEADERS = [c.name for c in CONTACT_COLUMNS]


def run_import(rows, *, headers=CONTACT_HEADERS, dry_run=False, resource=None):
    """Import `rows` (dicts keyed by column name; missing cells blank) as Excel rows 2.. .

    The outcome dicts (spec §3.4) are attached to the result as `result.outcomes`.
    """
    dataset = tablib.Dataset(headers=[*headers, "__row__"])
    for excel_row, row in enumerate(rows, start=2):
        dataset.append([row.get(h, "") for h in headers] + [excel_row])
    resource = resource or ContactResource()
    result = resource.import_data(
        dataset, dry_run=dry_run, use_transactions=True, rollback_on_validation_errors=False
    )
    assert not result.has_errors(), [e.error for r in result.row_errors() for e in r[1]]
    result.outcomes = [
        resource.outcome(row_result, dict(zip(dataset.headers, values, strict=True)))
        for row_result, values in zip(result.rows, dataset, strict=True)
    ]
    return result, resource


class _RaisingLoader(BaseInstanceLoader):
    def get_instance(self, row):
        raise ValidationError("nope")


class _ProbeResource(ModelResource):
    class Meta:
        model = Contact
        fields = ("name",)
        import_id_fields = []
        instance_loader_class = _RaisingLoader


def test_validation_error_from_loader_is_invalid():
    """Guards the library behaviour the matching design rests on (django-import-export 4.4)."""
    dataset = tablib.Dataset(["Jordan"], headers=["name"])
    result = _ProbeResource().import_data(dataset, dry_run=False, use_transactions=True)
    assert result.rows[0].import_type == RowResult.IMPORT_TYPE_INVALID
    assert not result.has_errors()


def test_creates_contact():
    result, _ = run_import([{"Name": "Jordan Smith", "Email": "Jordan@ACME.test"}])
    contact = Contact.objects.get()
    assert contact.name == "Jordan Smith"
    assert contact.email == "jordan@acme.test"
    assert contact.channel == Channel.WEBSITE
    out = result.outcomes[0]
    assert out["row"] == 2
    assert out["outcome"] == "new"
    assert out["label"] == "Jordan Smith"
    assert out["key"] == "jordan@acme.test"
    assert out["object_id"] == contact.pk
    assert out["changes"] == [["Name", "", "Jordan Smith"], ["Email", "", "jordan@acme.test"]]
    assert out["warnings"] == []


def test_dry_run_new_has_no_object_id_and_writes_nothing():
    result, _ = run_import([{"Name": "Jordan Smith", "Email": "jordan@acme.test"}], dry_run=True)
    assert result.outcomes[0]["outcome"] == "new"
    assert result.outcomes[0]["object_id"] is None
    assert not Contact.objects.exists()


def test_updates_by_email_mixed_case():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="", notes="")
    result, _ = run_import([{"Email": "Jordan@ACME.test", "Notes": "Prefers the Escalade"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["object_id"] == contact.pk
    assert out["key"] == "jordan@acme.test"
    assert out["label"] == "Jordan"
    assert out["changes"] == [["Notes", "", "Prefers the Escalade"]]
    contact.refresh_from_db()
    assert contact.notes == "Prefers the Escalade"
    assert contact.email == "jordan@acme.test"
    assert Contact.objects.count() == 1


def test_matches_phone_only_e164_and_legacy_raw():
    texter = ContactFactory(name="Texter", email=None, phone="+12025550143")
    legacy = ContactFactory(name="Legacy", email=None, phone="555-0199 ext 4")
    result, _ = run_import(
        [
            {"Phone": "(202) 555-0143", "Notes": "found by E.164"},
            {"Phone": "555-0199 ext 4", "Notes": "found as typed"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["updated", "updated"]
    assert [o["object_id"] for o in result.outcomes] == [texter.pk, legacy.pk]
    texter.refresh_from_db()
    legacy.refresh_from_db()
    assert texter.notes == "found by E.164"
    assert legacy.notes == "found as typed"
    assert Contact.objects.count() == 2


def test_new_email_fills_in_email_less_contact():
    contact = ContactFactory(name="Jordan", email=None, phone="+12025550143")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": "(202) 555-0143"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["object_id"] == contact.pk
    assert out["changes"] == [["Email", "", "jordan@acme.test"]]
    contact.refresh_from_db()
    assert contact.email == "jordan@acme.test"
    assert Contact.objects.count() == 1


def test_new_email_with_phone_on_other_email_creates_new_and_warns():
    pat = ContactFactory(name="Pat Lee", email="pat@other.test", phone="+12025550143")
    result, _ = run_import(
        [{"Name": "Jordan", "Email": "jordan@acme.test", "Phone": "(202) 555-0143"}]
    )
    out = result.outcomes[0]
    assert out["outcome"] == "new"
    assert out["warnings"] == [
        "Phone is also on Pat Lee (pat@other.test) — this creates a separate contact"
    ]
    assert Contact.objects.count() == 2
    jordan = Contact.objects.get(email="jordan@acme.test")
    assert out["object_id"] == jordan.pk
    pat.refresh_from_db()
    assert pat.email == "pat@other.test"


def test_no_key_is_an_error():
    result, _ = run_import([{"Name": "Jordan", "Notes": "hello"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Needs an email or a phone to match or create a contact."
    assert out["label"] == "Jordan"
    assert out["object_id"] is None
    assert out["values"]["Name"] == "Jordan"
    assert out["values"]["Notes"] == "hello"
    assert "__row__" not in out["values"]
    assert not Contact.objects.exists()


def test_duplicate_key_in_file():
    result, _ = run_import(
        [
            {"Name": "First", "Email": "jordan@acme.test"},
            {"Name": "Other", "Email": "other@acme.test"},
            {"Name": "Second", "Email": "JORDAN@acme.test"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["new", "new", "error"]
    assert result.outcomes[2]["row"] == 4
    assert result.outcomes[2]["reason"] == "Same email as row 2"
    assert Contact.objects.filter(email="jordan@acme.test").get().name == "First"


def test_duplicate_phone_in_file():
    result, _ = run_import(
        [
            {"Name": "First", "Phone": "(202) 555-0143"},
            {"Name": "Other", "Email": "other@acme.test"},
            {"Name": "Second", "Phone": "+12025550143"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["new", "new", "error"]
    assert result.outcomes[2]["reason"] == "Same phone as row 2"


def test_blank_never_clears_model_columns():
    contact = ContactFactory(
        name="Jordan",
        email="jordan@acme.test",
        phone="",
        notes="VIP",
        channel=Channel.WEDDING_PRO,
    )
    result, _ = run_import([{"Email": "jordan@acme.test", "Name": "  ", "Notes": ""}])
    out = result.outcomes[0]
    assert out["outcome"] == "unchanged"
    assert out["object_id"] == contact.pk
    assert out["changes"] == []
    contact.refresh_from_db()
    assert (contact.name, contact.notes, contact.channel) == ("Jordan", "VIP", "wedding_pro")


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("Wedding Pro", Channel.WEDDING_PRO),
        ("wedding_pro", Channel.WEDDING_PRO),
        ("PHONE", Channel.PHONE),
    ],
)
def test_channel_by_label_and_value(typed, stored):
    result, _ = run_import([{"Name": "Jordan", "Email": "jordan@acme.test", "Channel": typed}])
    assert Contact.objects.get().channel == stored
    label = Channel(stored).label
    assert ["Channel", "", label] in result.outcomes[0]["changes"]


def test_channel_change_shows_labels():
    ContactFactory(email="jordan@acme.test", phone="", channel=Channel.WEBSITE)
    result, _ = run_import([{"Email": "jordan@acme.test", "Channel": "phone"}])
    assert result.outcomes[0]["changes"] == [["Channel", "Website", "Phone"]]


def test_bad_channel_is_an_error():
    result, _ = run_import(
        [{"Name": "Jordan", "Email": "jordan@acme.test", "Channel": "Carrier pigeon"}]
    )
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == (
        "Channel 'Carrier pigeon' isn't one of: Website, Wedding Pro, Phone, API, GNet"
    )
    assert out["values"]["Channel"] == "Carrier pigeon"
    assert not Contact.objects.exists()


def test_name_required_on_new_row():
    result, _ = run_import([{"Email": "jordan@acme.test", "Notes": "no name"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Name is required for a new contact."
    assert not Contact.objects.exists()


def test_invalid_email_is_an_error():
    result, _ = run_import([{"Name": "Jordan", "Email": "not-an-email"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"].startswith("Email:")
    assert not Contact.objects.exists()


def test_error_row_does_not_stop_the_others():
    result, _ = run_import(
        [
            {"Name": "Jordan", "Email": "not-an-email"},
            {"Name": "Sam", "Email": "sam@acme.test"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["error", "new"]
    assert Contact.objects.get().email == "sam@acme.test"


def test_describe_names_columns_and_keeps_non_field_messages():
    error = ValidationError({"email": ["Enter a valid email address."], "__all__": ["Clash."]})
    text = PortalResource.describe(error, {"email": "Email"})
    assert text == "Email: Enter a valid email address. Clash."
    assert PortalResource.describe(ValidationError("Plain."), {}) == "Plain."


# --- One resource, one run at a time (carried from the Task 3 review) -----------------------


def test_dry_run_object_id_is_none_for_a_record_created_earlier_in_the_run():
    rows = [
        {"Name": "Jordan", "Email": "jordan@acme.test", "Phone": "(617) 555-0101"},
        {"Phone": "(617) 555-0101", "Notes": "found by the phone row 2 put on file"},
    ]
    result, _ = run_import(rows, dry_run=True)
    assert [o["outcome"] for o in result.outcomes] == ["new", "updated"]
    assert [o["object_id"] for o in result.outcomes] == [None, None]
    assert not Contact.objects.exists()

    result, _ = run_import(rows)
    jordan = Contact.objects.get()
    assert [o["object_id"] for o in result.outcomes] == [jordan.pk, jordan.pk]


def test_one_resource_runs_preview_then_apply():
    resource = ContactResource()
    first = [
        {"Name": "A", "Email": "a@acme.test"},
        {"Name": "B", "Email": "b@acme.test"},
        {"Name": "A again", "Email": "a@acme.test"},
    ]
    result, _ = run_import(first, dry_run=True, resource=resource)
    assert [o["outcome"] for o in result.outcomes] == ["new", "new", "error"]

    second = [
        {"Name": "C", "Email": "c@acme.test"},
        {"Name": "D", "Email": "d@acme.test"},
        {"Name": "E", "Email": "e@acme.test"},
    ]
    result, _ = run_import(second, resource=resource)
    assert [o["outcome"] for o in result.outcomes] == ["new", "new", "new"]
    assert [o["object_id"] for o in result.outcomes] == [
        Contact.objects.get(email=e).pk for e in ("c@acme.test", "d@acme.test", "e@acme.test")
    ]


def test_import_data_keeps_the_library_signature():
    dataset = tablib.Dataset(headers=[*CONTACT_HEADERS, "__row__"])
    dataset.append(["Jordan", "jordan@acme.test", *[""] * (len(CONTACT_HEADERS) - 2), 2])
    # dataset, dry_run, raise_errors, use_transactions — positionally, as the library allows.
    result = ContactResource().import_data(dataset, True, False, True)
    assert result.rows[0].import_type == RowResult.IMPORT_TYPE_NEW
    assert not Contact.objects.exists()


# --- Company (spec §5.1) ----------------------------------------------------------------------


def test_company_matched_case_insensitive_and_created():
    acme = CompanyFactory(name="Acme Events")
    result, _ = run_import(
        [
            {"Name": "Jordan", "Email": "jordan@acme.test", "Company": "acme events"},
            {"Name": "Sam", "Email": "sam@acme.test", "Company": "New Co"},
        ]
    )
    assert Company.objects.count() == 2
    assert Contact.objects.get(email="jordan@acme.test").company == acme
    assert Contact.objects.get(email="sam@acme.test").company.name == "New Co"
    assert ["Company", "", "Acme Events"] in result.outcomes[0]["changes"]
    assert ["Company", "", "New Co"] in result.outcomes[1]["changes"]


def test_company_same_name_other_case_is_unchanged_and_a_change_is_listed():
    old = CompanyFactory(name="Acme Events")
    CompanyFactory(name="Bright Weddings")
    contact = ContactFactory(email="jordan@acme.test", phone="", company=old)
    result, _ = run_import([{"Email": "jordan@acme.test", "Company": "ACME EVENTS"}])
    assert result.outcomes[0]["outcome"] == "unchanged"

    result, _ = run_import([{"Email": "jordan@acme.test", "Company": "bright weddings"}])
    assert result.outcomes[0]["changes"] == [["Company", "Acme Events", "Bright Weddings"]]
    contact.refresh_from_db()
    assert contact.company.name == "Bright Weddings"
    assert Company.objects.count() == 2


def test_company_name_too_long_is_an_error():
    result, _ = run_import([{"Name": "Jordan", "Email": "jordan@acme.test", "Company": "x" * 201}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Company: Ensure this value has at most 200 characters (it has 201)."
    assert not Company.objects.exists()


# --- Nothing is written by a preview or by an invalid row (spec §7) -------------------------


def _counts() -> list[int]:
    return [m.objects.count() for m in (Contact, ContactPhone, Company, Address, CustomerLink)]


def test_preview_writes_nothing():
    pat = ContactFactory(name="Pat", email="pat@acme.test", phone="+12025550143", notes="")
    link = CustomerLinkFactory(contact=pat, display_name="Pat", qbo_sync_state=SyncState.SYNCED)
    before = _counts()
    result, _ = run_import(
        [
            {
                "Name": "Jordan",
                "Email": "jordan@acme.test",
                "Phone": "(617) 555-0101",
                "Other phones": "(202) 555-0199 (Work)",
                "Company": "New Co",
                "Street": "1 Main St",
                "City": "Boston",
            },
            {
                "Email": "pat@acme.test",
                "Name": "Pat Lee",
                "Phone": "(617) 555-0102",
                "Other phones": "617-555-0103",
                "Company": "Other Co",
                "Street": "2 Elm St",
                "Notes": "VIP",
            },
        ],
        dry_run=True,
    )
    assert [o["outcome"] for o in result.outcomes] == ["new", "updated"]
    assert _counts() == before
    pat.refresh_from_db()
    assert (pat.name, pat.phone, pat.company, pat.primary_address, pat.notes) == (
        "Pat",
        "+12025550143",
        None,
        None,
        "",
    )
    link.refresh_from_db()
    assert (link.display_name, link.qbo_sync_state) == ("Pat", SyncState.SYNCED)


def test_invalid_row_creates_no_company_or_address():
    pat = ContactFactory(name="Pat", email="pat@acme.test", phone="")
    before = Address.objects.count()
    result, _ = run_import(
        [
            {
                "Name": "Ghost",
                "Email": "ghost@acme.test",
                "Company": "Ghost Co",
                "Street": "1 Main",
                "Other phones": "nonsense",
            },
            {
                "Email": "pat@acme.test",
                "Company": "Ghost Two",
                "Street": "2 Main",
                "Phone": "(617) 555-0101",
                "Other phones": "(202) 555-0199, nonsense",
            },
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["error", "error"]
    assert result.outcomes[0]["reason"] == (
        "Other phones: 'nonsense' isn't a phone number we can dial."
    )
    assert not Company.objects.filter(name__in=["Ghost Co", "Ghost Two"]).exists()
    assert Address.objects.count() == before
    assert not Contact.objects.filter(email="ghost@acme.test").exists()
    pat.refresh_from_db()
    assert (pat.company, pat.primary_address, pat.phone) == (None, None, "")
    assert not ContactPhone.objects.filter(contact=pat).exists()


# --- Phone (spec §5.3) ----------------------------------------------------------------------


def test_new_contact_phone_is_the_texting_number():
    result, _ = run_import(
        [{"Name": "Jordan", "Email": "jordan@acme.test", "Phone": "202.555.0143"}]
    )
    contact = Contact.objects.get()
    assert contact.phone == "+12025550143"
    assert contact.phones.get().texting
    assert ["Phone", "", "(202) 555-0143"] in result.outcomes[0]["changes"]


def test_phone_on_update_is_added_not_texting():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": "(617) 555-0101"}])
    out = result.outcomes[0]
    assert out["outcome"] == "updated"
    assert out["changes"] == [["Other phones", "", "+ (617) 555-0101"]]
    contact.refresh_from_db()
    assert contact.phone == "+12025550143"
    added = ContactPhone.objects.get(contact=contact, number="+16175550101")
    assert not added.texting
    assert ContactPhone.objects.get(contact=contact, number="+12025550143").texting


def test_phone_on_phone_less_contact_becomes_texting():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": "(617) 555-0101"}])
    assert result.outcomes[0]["changes"] == [["Phone", "", "(617) 555-0101"]]
    contact.refresh_from_db()
    assert contact.phone == "+16175550101"
    assert ContactPhone.objects.get(contact=contact).texting


@pytest.mark.parametrize("typed", ["(202) 555-0143", "+12025550143", "202-555-0143"])
def test_same_phone_in_display_form_is_unchanged(typed):
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": typed}])
    assert result.outcomes[0]["outcome"] == "unchanged"
    assert result.outcomes[0]["changes"] == []
    assert ContactPhone.objects.count() == 1


def test_legacy_raw_phone_is_unchanged():
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="555-0199 ext 4")
    ContactFactory(name="Legacy", email=None, phone="555-0177 ext 2")
    result, _ = run_import(
        [
            {"Email": "jordan@acme.test", "Phone": "555-0199 ext 4"},
            {"Phone": "555-0177 ext 2"},
        ]
    )
    assert [o["outcome"] for o in result.outcomes] == ["unchanged", "unchanged"]
    assert ContactPhone.objects.count() == 2


def test_phone_already_on_file_as_another_number_is_unchanged():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    ContactPhone.objects.create(contact=contact, number="+16175550101", label="work")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": "(617) 555-0101"}])
    assert result.outcomes[0]["outcome"] == "unchanged"


def test_bad_phone_on_new_contact_is_an_error():
    result, _ = run_import([{"Name": "Jordan", "Email": "jordan@acme.test", "Phone": "12345"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Phone '12345' isn't a phone number we can dial."
    assert not Contact.objects.exists()


def test_bad_different_phone_on_update_is_an_error():
    contact = ContactFactory(email="jordan@acme.test", phone="+12025550143", notes="")
    result, _ = run_import([{"Email": "jordan@acme.test", "Phone": "12345", "Notes": "x"}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "Phone '12345' isn't a phone number we can dial."
    contact.refresh_from_db()
    assert contact.notes == ""


# --- Other phones (spec §5.3) ---------------------------------------------------------------


def _phones(contact) -> dict[str, tuple[str, bool]]:
    return {p.number: (p.label, p.texting) for p in ContactPhone.objects.filter(contact=contact)}


def test_other_phones_add_only_with_labels():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    ContactPhone.objects.create(contact=contact, number="+13015550100", label="home")
    result, _ = run_import(
        [{"Email": "jordan@acme.test", "Other phones": "(202) 555-0199 (Work); 617-555-0102"}]
    )
    assert result.outcomes[0]["changes"] == [
        ["Other phones", "", "+ (202) 555-0199 (Work)"],
        ["Other phones", "", "+ (617) 555-0102"],
    ]
    assert _phones(contact) == {
        "+12025550143": ("mobile", True),
        "+13015550100": ("home", False),
        "+12025550199": ("work", False),
        "+16175550102": ("mobile", False),
    }


def test_other_phones_label_update():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    ContactPhone.objects.create(contact=contact, number="+12025550199", label="mobile")
    result, _ = run_import([{"Email": "jordan@acme.test", "Other phones": "(202) 555-0199 (Home)"}])
    assert result.outcomes[0]["changes"] == [
        ["Other phones", "(202) 555-0199 (Mobile)", "(202) 555-0199 (Home)"]
    ]
    assert _phones(contact)["+12025550199"] == ("home", False)


def test_other_phones_on_file_without_a_label_is_unchanged():
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    ContactPhone.objects.create(contact=contact, number="+12025550199", label="work")
    ContactPhone.objects.create(contact=contact, number="555-0177 ext 2", label="home")
    result, _ = run_import(
        [
            {
                "Email": "jordan@acme.test",
                "Other phones": "202-555-0199, 555-0177 ext 2 (Home), (202) 555-0143 (mobile)",
            }
        ]
    )
    assert result.outcomes[0]["outcome"] == "unchanged"


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("(202) 555-0199, 12345", "Other phones: '12345' isn't a phone number we can dial."),
        ("(202) 555-0199 (Cell)", "Phone label 'Cell' isn't one of: Mobile, Work, Home, Other"),
    ],
)
def test_bad_other_phone_is_an_error(typed, reason):
    contact = ContactFactory(name="Jordan", email="jordan@acme.test", phone="+12025550143")
    result, _ = run_import([{"Email": "jordan@acme.test", "Other phones": typed}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == reason
    assert list(_phones(contact)) == ["+12025550143"]


def test_new_contact_other_phones_listed_formatted():
    result, _ = run_import(
        [
            {
                "Name": "Jordan",
                "Email": "jordan@acme.test",
                "Phone": "(202) 555-0143",
                "Other phones": "202.555.0199 (work)",
            }
        ]
    )
    assert result.outcomes[0]["changes"] == [
        ["Name", "", "Jordan"],
        ["Email", "", "jordan@acme.test"],
        ["Phone", "", "(202) 555-0143"],
        ["Other phones", "", "(202) 555-0199 (Work)"],
    ]
    contact = Contact.objects.get()
    assert _phones(contact) == {
        "+12025550143": ("mobile", True),
        "+12025550199": ("work", False),
    }


# --- Address (spec §5.4) --------------------------------------------------------------------


ADDRESS_ROW = {
    "Street": "1600 Pennsylvania Ave NW",
    "Street 2": "Suite 1",
    "City": "Washington",
    "State": "DC",
    "ZIP": "20500",
}


def test_address_created_as_typed():
    result, _ = run_import([{"Name": "Jordan", "Email": "jordan@acme.test", **ADDRESS_ROW}])
    address = Contact.objects.get().primary_address
    assert (address.line1, address.line2, address.city, address.state, address.postal) == (
        "1600 Pennsylvania Ave NW",
        "Suite 1",
        "Washington",
        "DC",
        "20500",
    )
    assert address.country == "United States"
    assert address.latitude is None
    assert ["ZIP", "", "20500"] in result.outcomes[0]["changes"]

    sam = ContactFactory(email="sam@acme.test", phone="")
    result, _ = run_import([{"Email": "sam@acme.test", "City": "Boston", "ZIP": "02134"}])
    assert result.outcomes[0]["changes"] == [["City", "", "Boston"], ["ZIP", "", "02134"]]
    sam.refresh_from_db()
    assert (sam.primary_address.city, sam.primary_address.postal, sam.primary_address.line1) == (
        "Boston",
        "02134",
        "",
    )


def test_address_change_clears_coordinates():
    address = AddressFactory(
        line1="1 Old St",
        city="Boston",
        state="MA",
        postal="02134",
        country="United States",
        latitude=Decimal("42.350000"),
        longitude=Decimal("-71.050000"),
        locationiq_place_id="123",
        display_name="1 Old St, Boston",
    )
    contact = ContactFactory(email="jordan@acme.test", phone="", primary_address=address)

    result, _ = run_import([{"Email": "jordan@acme.test", "City": "Boston"}])
    assert result.outcomes[0]["outcome"] == "unchanged"
    address.refresh_from_db()
    assert address.latitude == Decimal("42.350000")

    result, _ = run_import([{"Email": "jordan@acme.test", "Street": "2 New St", "City": "Boston"}])
    assert result.outcomes[0]["changes"] == [["Street", "1 Old St", "2 New St"]]
    address.refresh_from_db()
    assert (address.line1, address.city, address.state, address.postal) == (
        "2 New St",
        "Boston",
        "MA",
        "02134",
    )
    assert (address.latitude, address.longitude, address.locationiq_place_id) == (None, None, "")
    assert address.display_name == ""
    assert address.country == "United States"
    contact.refresh_from_db()
    assert contact.primary_address_id == address.pk


def test_address_cell_too_long_is_an_error():
    result, _ = run_import([{"Name": "Jordan", "Email": "jordan@acme.test", "ZIP": "1" * 21}])
    out = result.outcomes[0]
    assert out["outcome"] == "error"
    assert out["reason"] == "ZIP: Ensure this value has at most 20 characters (it has 21)."
    assert not Address.objects.exists()


def test_blank_never_clears_virtual_columns():
    company = CompanyFactory(name="Acme Events")
    address = AddressFactory(line1="1 Main St", city="Boston")
    contact = ContactFactory(
        email="jordan@acme.test", phone="+12025550143", company=company, primary_address=address
    )
    ContactPhone.objects.create(contact=contact, number="+12025550199", label="work")
    result, _ = run_import([{"Email": "jordan@acme.test"}])
    assert result.outcomes[0]["outcome"] == "unchanged"
    contact.refresh_from_db()
    assert (contact.company, contact.primary_address, contact.phone) == (
        company,
        address,
        "+12025550143",
    )
    assert set(_phones(contact)) == {"+12025550143", "+12025550199"}
    address.refresh_from_db()
    assert (address.line1, address.city) == ("1 Main St", "Boston")


# --- Side effects (spec §5.5) ---------------------------------------------------------------


def test_company_change_refreshes_farm_in():
    contact = ContactFactory(email="jordan@acme.test", phone="", company=CompanyFactory(name="Old"))
    with mock.patch("apps.tasks.services.refresh_farm_in_for_contact") as refresh:
        run_import([{"Email": "jordan@acme.test", "Company": "New Co"}], dry_run=True)
        refresh.assert_not_called()
        run_import([{"Email": "jordan@acme.test", "Notes": "notes only"}])
        refresh.assert_not_called()
        run_import([{"Email": "jordan@acme.test", "Company": "New Co"}])
    refresh.assert_called_once()
    assert refresh.call_args.args[0].pk == contact.pk


def test_rename_requeues_customer_link():
    contact = ContactFactory(name="Ada", email="ada@acme.test", phone="")
    link = CustomerLinkFactory(
        contact=contact,
        display_name="Ada",
        qbo_id="55",
        qbo_realm_id="123",
        qbo_sync_state=SyncState.SYNCED,
    )
    run_import([{"Email": "ada@acme.test", "Notes": "VIP"}])
    link.refresh_from_db()
    assert link.qbo_sync_state == SyncState.SYNCED

    run_import([{"Email": "ada@acme.test", "Name": "Ada King"}])
    link.refresh_from_db()
    assert (link.display_name, link.qbo_sync_state) == ("Ada King", SyncState.NOT_SYNCED)


def test_import_sends_nothing():
    from apps.leads.models import Lead
    from apps.messaging.models import Conversation, TouchPoint

    ContactFactory(name="Pat", email="pat@acme.test", phone="+12025550143")
    ContactFactory(name="Texter", email=None, phone="+12025550144")
    with (
        mock.patch("apps.integrations.podium.send_message") as send_message,
        mock.patch("apps.integrations.podium.create_review_invitation") as invite,
        mock.patch("apps.notifications.email.send_html_email") as send_html_email,
        mock.patch("apps.core.outbound.allowed") as allowed,
        mock.patch("django.core.mail.send_mail") as send_mail,
    ):
        result, _ = run_import(
            [
                {
                    "Name": "Jordan",
                    "Email": "jordan@acme.test",
                    "Phone": "(617) 555-0101",
                    "Company": "New Co",
                    "Street": "1 Main St",
                },
                {"Email": "pat@acme.test", "Name": "Pat Lee", "Phone": "(617) 555-0102"},
                {"Email": "texter@acme.test", "Phone": "(202) 555-0144"},
            ]
        )
    assert [o["outcome"] for o in result.outcomes] == ["new", "updated", "updated"]
    for sender in (send_message, invite, send_html_email, allowed, send_mail):
        sender.assert_not_called()
    assert (Lead.objects.count(), Conversation.objects.count(), TouchPoint.objects.count()) == (
        0,
        0,
        0,
    )
    assert len(mail.outbox) == 0


# A row that changes Notes and adds a phone measured 9 queries (savepoint + release, match, phones
# on file, the email constraint check, billing pre_save, update, add_phone's lookup + insert), the
# run 6 more (50 rows: 456, 100: 906).
# 100 rows must fit the same constants as 50: the cost per row does not grow with the file.
QUERY_BASE = 10
QUERY_PER_ROW = 10


@pytest.mark.parametrize("n", [50, 100])
def test_query_count_is_linear(n, django_assert_max_num_queries):
    for i in range(n):
        ContactFactory(name=f"C{i}", email=f"c{i}@acme.test", phone=f"+1202555{1000 + i}")
    rows = [
        {"Email": f"c{i}@acme.test", "Notes": "updated", "Phone": f"(617) 555-{1000 + i}"}
        for i in range(n)
    ]
    with django_assert_max_num_queries(QUERY_BASE + n * QUERY_PER_ROW):
        result, _ = run_import(rows)
    assert {o["outcome"] for o in result.outcomes} == {"updated"}
    assert ContactPhone.objects.count() == 2 * n


# --- Export (spec §4.2, §5.1) ---------------------------------------------------------------


def test_export_virtual_columns():
    from apps.contacts import services

    company = CompanyFactory(name="Acme Events")
    address = AddressFactory(
        line1="1 Main St", line2="Suite 2", city="Boston", state="MA", postal="02134"
    )
    contact = ContactFactory(
        name="Jordan",
        email="jordan@acme.test",
        phone="+12025550143",
        company=company,
        primary_address=address,
        channel=Channel.WEDDING_PRO,
        notes="VIP",
    )
    services.add_phone(contact, "(202) 555-0199", label="work")
    services.add_phone(contact, "617-555-0102")
    ContactFactory(name="Bare", email="bare@acme.test", phone="", notes="")

    queryset = ContactResource.export_queryset(Contact.objects.order_by("name"))
    dataset = ContactResource().export(queryset=queryset)
    assert dataset.headers == CONTACT_HEADERS
    assert dataset[0] == ("Bare", "bare@acme.test", "", "", "", "Website", *[""] * 5, "")
    assert dataset[1] == (
        "Jordan",
        "jordan@acme.test",
        "(202) 555-0143",
        "(202) 555-0199 (Work), (617) 555-0102 (Mobile)",
        "Acme Events",
        "Wedding Pro",
        "1 Main St",
        "Suite 2",
        "Boston",
        "MA",
        "02134",
        "VIP",
    )


def test_export_query_count_is_fixed(django_assert_num_queries):
    for i in range(5):
        contact = ContactFactory(
            email=f"c{i}@acme.test",
            phone=f"+1202555{1000 + i}",
            company=CompanyFactory(),
            primary_address=AddressFactory(line1=f"{i} Main St"),
        )
        ContactPhone.objects.create(contact=contact, number=f"+1617555{1000 + i}")
    queryset = ContactResource.export_queryset(Contact.objects.all())
    # The library counts the rows, then contacts with company + address, then the phones.
    with django_assert_num_queries(3):
        dataset = ContactResource().export(queryset=queryset)
    assert len(dataset) == 5
