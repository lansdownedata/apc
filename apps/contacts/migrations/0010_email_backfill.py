from django.db import migrations


def forwards(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    for pk, email in Contact.objects.values_list("pk", "email"):
        norm = (email or "").strip().lower() or None
        if norm != email:
            Contact.objects.filter(pk=pk).update(email=norm)


class Migration(migrations.Migration):
    dependencies = [("contacts", "0009_email_nullable")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
