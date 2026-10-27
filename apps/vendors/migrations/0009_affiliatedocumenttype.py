import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0008_vendor_qbo_lease"),
    ]

    operations = [
        migrations.CreateModel(
            name="AffiliateDocumentType",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80, unique=True)),
                ("required", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("active", models.BooleanField(default=True)),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.AddField(
            model_name="vendordocument",
            name="document_type",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="vendors.affiliatedocumenttype",
            ),
        ),
    ]
