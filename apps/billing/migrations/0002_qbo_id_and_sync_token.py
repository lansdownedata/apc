from django.db import migrations, models


class Migration(migrations.Migration):
    """APC-42: `qbo_customer_id` → the neutral `qbo_id` (APC-74 reuses the mixin for vendors and
    bills), plus the SyncToken every QuickBooks update must send. A rename, not drop + add —
    no QuickBooks link is lost."""

    dependencies = [("billing", "0001_initial")]

    operations = [
        migrations.RenameField("billingaccount", "qbo_customer_id", "qbo_id"),
        migrations.RenameField("accountgroup", "qbo_customer_id", "qbo_id"),
        migrations.AddField(
            model_name="billingaccount",
            name="qbo_sync_token",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="accountgroup",
            name="qbo_sync_token",
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
