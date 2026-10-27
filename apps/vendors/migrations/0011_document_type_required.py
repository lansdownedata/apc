import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0010_seed_document_types"),
    ]

    operations = [
        migrations.RemoveField(model_name="vendordocument", name="kind"),
        migrations.AlterField(
            model_name="vendordocument",
            name="document_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="vendors.affiliatedocumenttype",
            ),
        ),
    ]
