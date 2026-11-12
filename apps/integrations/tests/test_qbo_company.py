"""Intuit's security requirements: the QuickBooks company id (realmID) is stored encrypted.

The credential keeps it encrypted, for the API URLs. Synced records only need to know which
company they were pushed to, so they carry an opaque company key instead (`QboCompany` maps
the key to the encrypted id), and the plain id is stored nowhere.
"""

import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.billing import qbo_sync
from apps.integrations.models import QboCompany, QboCredential

pytestmark = pytest.mark.django_db


def _connect(realm: str) -> QboCredential:
    cred = QboCredential.load()
    cred.realm_id = realm
    cred.save()
    return cred


def test_the_company_id_is_stored_encrypted():
    _connect("9130")

    row = QboCredential.objects.values().get(singleton_id=1)

    assert "9130" not in json.dumps(row, default=str)
    assert QboCredential.load().realm_id == "9130"


def test_records_are_stamped_with_a_company_key_not_the_company_id():
    _connect("9130")

    key = qbo_sync.current_realm()

    assert key and "9130" not in key


def test_the_company_map_is_encrypted_too():
    _connect("9130")

    assert "9130" not in json.dumps(list(QboCompany.objects.values()), default=str)
    assert QboCompany.objects.get().realm_id == "9130"


def test_reconnecting_the_same_company_keeps_its_key():
    _connect("9130")
    first = qbo_sync.current_realm()

    _connect("5555")
    other = qbo_sync.current_realm()
    _connect("9130")

    assert other != first
    assert qbo_sync.current_realm() == first
    assert QboCompany.objects.count() == 2


def test_disconnecting_clears_the_company():
    _connect("9130")

    _connect("")

    cred = QboCredential.load()
    assert (cred.realm_id, cred.realm_id_enc, qbo_sync.current_realm()) == ("", "", "")


def test_the_connection_never_shows_its_company_id_in_its_name():
    assert "9130" not in str(_connect("9130"))


# --- the migration that swaps stored ids for company keys ------------------------------------

BEFORE = [
    ("integrations", "0007_qbo_credential"),
    ("billing", "0014_terms_free_text"),
    ("payments", "0020_charge_hold_kind"),
    ("vendors", "0011_document_type_required"),
]


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_the_migration_swaps_every_stored_company_id_for_a_key_and_back(restore_seeded_rows):
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    old.get_model("integrations", "QboCredential").objects.create(singleton_id=1, realm_id="9130")
    old.get_model("billing", "QboTerm").objects.create(realm_id="9130", qbo_id="3", name="Net 30")
    old.get_model("billing", "QboTerm").objects.create(realm_id="777", qbo_id="3", name="Net 30")
    old.get_model("vendors", "Vendor").objects.create(name="Acme Limo", qbo_realm_id="9130")

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    from apps.billing.models import QboTerm
    from apps.vendors.models import Vendor

    key = qbo_sync.current_realm()
    assert QboCredential.load().realm_id == "9130"
    assert set(QboTerm.objects.values_list("realm_id", flat=True)) == {
        key,
        QboCompany.objects.exclude(key=key).get().key,
    }
    assert Vendor.objects.get().qbo_realm_id == key
    assert {c.realm_id for c in QboCompany.objects.all()} == {"9130", "777"}

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    assert old.get_model("integrations", "QboCredential").objects.get().realm_id == "9130"
    assert set(old.get_model("billing", "QboTerm").objects.values_list("realm_id", flat=True)) == {
        "9130",
        "777",
    }

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
