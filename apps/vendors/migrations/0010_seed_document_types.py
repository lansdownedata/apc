from django.db import migrations

SEED = [
    ("DOT operating authority", True, 10),
    ("W-9", True, 20),
    ("Affiliate agreement", True, 30),
    ("Other", False, 100),
]

KIND_TO_TYPE = {
    "operating_authority": "DOT operating authority",
    "w9": "W-9",
    "affiliate_agreement": "Affiliate agreement",
    "other": "Other",
}


def type_name_for(kind: str) -> str:
    """The seeded type a legacy `VendorDocument.kind` value maps to; unknown -> Other."""
    return KIND_TO_TYPE.get(kind, "Other")


def seed_and_map(apps, schema_editor):
    Type = apps.get_model("vendors", "AffiliateDocumentType")
    Document = apps.get_model("vendors", "VendorDocument")
    by_name = {}
    for name, required, sort_order in SEED:
        by_name[name], _ = Type.objects.get_or_create(
            name=name, defaults={"required": required, "sort_order": sort_order}
        )
    for kind in set(Document.objects.values_list("kind", flat=True)):
        Document.objects.filter(kind=kind).update(document_type=by_name[type_name_for(kind)])


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0009_affiliatedocumenttype"),
    ]

    operations = [
        migrations.RunPython(seed_and_map, migrations.RunPython.noop),
    ]
