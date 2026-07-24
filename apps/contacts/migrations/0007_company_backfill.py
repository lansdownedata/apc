from django.db import migrations


def forwards(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    Company = apps.get_model("contacts", "Company")
    for pk, legacy in Contact.objects.exclude(company_legacy="").values_list("pk", "company_legacy"):
        name = (legacy or "").strip()
        if not name:
            continue
        company = Company.objects.filter(name__iexact=name).first() or Company.objects.create(name=name)
        Contact.objects.filter(pk=pk).update(company=company)


def backwards(apps, schema_editor):
    """No-op — company_legacy is dropped next; Company names still hold the strings."""


class Migration(migrations.Migration):
    dependencies = [("contacts", "0006_company_fk")]

    operations = [migrations.RunPython(forwards, backwards)]
