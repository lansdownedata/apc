from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0009_encrypt_realm_ids"),
    ]

    operations = [
        migrations.RemoveField(model_name="qbocredential", name="realm_id"),
    ]
