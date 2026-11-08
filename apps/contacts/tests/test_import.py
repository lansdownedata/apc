"""Contact import: matching (spec §5.2) and the model columns (§5.1). APC-141."""

import pytest
import tablib
from django.core.exceptions import ValidationError
from import_export.instance_loaders import BaseInstanceLoader
from import_export.resources import ModelResource
from import_export.results import RowResult

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.contacts.resources import CONTACT_COLUMNS, ContactResource
from apps.core.choices import Channel
from apps.imports.resources import PortalResource

pytestmark = pytest.mark.django_db

CONTACT_HEADERS = [c.name for c in CONTACT_COLUMNS]


def run_import(rows, *, headers=CONTACT_HEADERS, dry_run=False):
    """Import `rows` (dicts keyed by column name; missing cells blank) as Excel rows 2.. .

    The outcome dicts (spec §3.4) are attached to the result as `result.outcomes`.
    """
    dataset = tablib.Dataset(headers=[*headers, "__row__"])
    for excel_row, row in enumerate(rows, start=2):
        dataset.append([row.get(h, "") for h in headers] + [excel_row])
    resource = ContactResource()
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
