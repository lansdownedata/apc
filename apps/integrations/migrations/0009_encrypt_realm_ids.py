"""Intuit: the QuickBooks company id (realmID) is stored encrypted.

Every plain id in the database becomes a `QboCompany` (the id encrypted, under an opaque
key); the credential gets the encrypted id plus the key, and every synced record's
`*realm_id` column gets the key. Reversible: the reverse decrypts the ids back in place.
"""

import uuid

from django.db import migrations

from apps.integrations import crypto

# Every column that holds a company id, as of this migration.
REALM_COLUMNS = (
    ("billing", "BillingAccount", "qbo_realm_id"),
    ("billing", "AccountGroup", "qbo_realm_id"),
    ("billing", "Invoice", "qbo_realm_id"),
    ("billing", "CustomerLink", "qbo_realm_id"),
    ("billing", "QboTerm", "realm_id"),
    ("billing", "QboAccount", "realm_id"),
    ("billing", "QboItem", "realm_id"),
    ("payments", "Charge", "qbo_realm_id"),
    ("payments", "AffiliatePayable", "qbo_realm_id"),
    ("payments", "AffiliatePayable", "qbo_payment_realm_id"),
    ("payments", "AffiliatePayable", "qbo_attachment_realm_id"),
    ("vendors", "Vendor", "qbo_realm_id"),
)


def _swap(apps, mapping: dict[str, str]) -> None:
    for app_label, model_name, field in REALM_COLUMNS:
        model = apps.get_model(app_label, model_name)
        for old, new in mapping.items():
            model.objects.filter(**{field: old}).update(**{field: new})


def encrypt_ids(apps, schema_editor):
    QboCompany = apps.get_model("integrations", "QboCompany")
    QboCredential = apps.get_model("integrations", "QboCredential")
    ids = set(QboCredential.objects.exclude(realm_id="").values_list("realm_id", flat=True))
    for app_label, model_name, field in REALM_COLUMNS:
        model = apps.get_model(app_label, model_name)
        ids |= set(model.objects.exclude(**{field: ""}).values_list(field, flat=True))
    keys = {}
    for realm_id in sorted(ids):
        keys[realm_id] = uuid.uuid4().hex
        QboCompany.objects.create(key=keys[realm_id], realm_id_enc=crypto.encrypt(realm_id))
    _swap(apps, keys)
    for cred in QboCredential.objects.exclude(realm_id=""):
        cred.realm_id_enc = crypto.encrypt(cred.realm_id)
        cred.company_key = keys[cred.realm_id]
        cred.realm_id = ""
        cred.save(update_fields=["realm_id_enc", "company_key", "realm_id"])


def decrypt_ids(apps, schema_editor):
    QboCompany = apps.get_model("integrations", "QboCompany")
    QboCredential = apps.get_model("integrations", "QboCredential")
    ids = {c.key: crypto.decrypt(c.realm_id_enc) for c in QboCompany.objects.all()}
    _swap(apps, ids)
    for cred in QboCredential.objects.exclude(realm_id_enc=""):
        cred.realm_id = crypto.decrypt(cred.realm_id_enc)
        cred.realm_id_enc = cred.company_key = ""
        cred.save(update_fields=["realm_id_enc", "company_key", "realm_id"])
    QboCompany.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0008_qbo_company"),
        ("billing", "0014_terms_free_text"),
        ("payments", "0020_charge_hold_kind"),
        ("vendors", "0011_document_type_required"),
    ]

    operations = [migrations.RunPython(encrypt_ids, decrypt_ids)]
