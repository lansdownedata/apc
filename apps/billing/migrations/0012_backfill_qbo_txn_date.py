from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    """QuickBooks holds every invoice pushed before 0011 under its own date: none could be
    edited before APC-109, so the date QuickBooks has is the invoice's."""
    Invoice = apps.get_model("billing", "Invoice")
    Invoice.objects.exclude(qbo_id="").filter(qbo_txn_date__isnull=True).update(
        qbo_txn_date=F("invoice_date")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0011_invoice_qbo_txn_date"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
