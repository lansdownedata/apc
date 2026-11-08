from django.db import migrations, models


def fill_realm(apps, schema_editor):
    """Terms cached so far came from the one connected company: stamp them with its id
    (left blank when nothing is connected; the next refresh re-reads them under the right one)."""
    Credential = apps.get_model("integrations", "QboCredential")
    QboTerm = apps.get_model("billing", "QboTerm")
    credential = Credential.objects.first()
    if credential is not None and credential.realm_id:
        QboTerm.objects.filter(realm_id="").update(realm_id=credential.realm_id)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0012_backfill_qbo_txn_date"),
        ("integrations", "0007_qbo_credential"),
    ]

    operations = [
        migrations.AlterField(
            model_name="qboterm",
            name="qbo_id",
            field=models.CharField(max_length=32),
        ),
        migrations.AddField(
            model_name="qboterm",
            name="realm_id",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="qboterm",
            name="term_type",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="qboterm",
            name="due_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qboterm",
            name="day_of_month_due",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="qboterm",
            name="due_next_month_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="qboterm",
            constraint=models.UniqueConstraint(fields=("realm_id", "qbo_id"), name="uniq_qbo_term"),
        ),
        migrations.RunPython(fill_realm, migrations.RunPython.noop),
    ]
