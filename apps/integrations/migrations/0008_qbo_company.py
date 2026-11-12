from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0007_qbo_credential"),
    ]

    operations = [
        migrations.CreateModel(
            name="QboCompany",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=32, unique=True)),
                ("realm_id_enc", models.TextField()),
            ],
            options={
                "verbose_name": "QuickBooks company",
                "verbose_name_plural": "QuickBooks companies",
            },
        ),
        migrations.AddField(
            model_name="qbocredential",
            name="company_key",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="qbocredential",
            name="realm_id_enc",
            field=models.TextField(blank=True),
        ),
    ]
