"""Move every stored secret to the current FIELD_ENCRYPTION_KEY.

    python manage.py reencrypt_fields

Run once after FIELD_ENCRYPTION_KEY is first set (values written before it used a key derived
from SECRET_KEY), and after each rotation (new key first in the list). Safe to re-run: a value
already under the current key is left alone. Rows are locked while they are rewritten, so a
QuickBooks token refresh running at the same moment cannot write a stale token back.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.integrations import crypto
from apps.integrations.models import LACustomer, QboCompany, QboCredential

ENCRYPTED_FIELDS = (
    (LACustomer, ("password_encrypted",)),
    (QboCredential, ("access_token_enc", "refresh_token_enc", "realm_id_enc")),
    (QboCompany, ("realm_id_enc",)),
)


class Command(BaseCommand):
    help = "Re-encrypt stored secrets under the current FIELD_ENCRYPTION_KEY."

    def handle(self, *args, **options):
        moved = 0
        for model, fields in ENCRYPTED_FIELDS:
            with transaction.atomic():
                for row in model.objects.select_for_update().order_by("pk"):
                    stale = [
                        f
                        for f in fields
                        if getattr(row, f) and not crypto.is_current(getattr(row, f))
                    ]
                    for field in stale:
                        setattr(row, field, crypto.rotate(getattr(row, field)))
                    if stale:
                        row.save(update_fields=stale)
                        moved += len(stale)
        self.stdout.write(self.style.SUCCESS(f"{moved} re-encrypted."))
