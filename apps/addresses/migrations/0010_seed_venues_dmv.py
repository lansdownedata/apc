# Re-runs load_venues after the venue directory grew from the ~44 Loudoun/Fauquier
# rows to a DMV-wide list (DC + Maryland suburbs + Northern Virginia). load_venues is
# update_or_create on (name, kind), so this only adds the new rows and leaves the
# originals — including their lead_hits history — untouched. A deploy carries it because
# Heroku runs `migrate` on release and nothing else (see 0009_seed_venues).

from django.db import migrations

from apps.addresses.loaders import load_venues


def seed(apps, schema_editor):
    load_venues(apps.get_model("addresses", "Venue"))


def unseed(apps, schema_editor):
    # load_venues only upserts; there is no safe automatic way to un-add just the new
    # rows without also dropping curated coordinates/caps. Reverse is a deliberate no-op.
    pass


class Migration(migrations.Migration):
    dependencies = [("addresses", "0009_seed_venues")]
    operations = [migrations.RunPython(seed, unseed)]
