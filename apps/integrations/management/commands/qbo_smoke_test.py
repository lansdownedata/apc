"""APC-41 — prove the QuickBooks connection works: fetch CompanyInfo and print it.

    python manage.py qbo_smoke_test

The QuickBooks counterpart of `la_smoke_test`. Read-only. Prints the company, its id and the
environment — never a token. Connect first from /portal/settings/quickbooks/.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.integrations import qbo


class Command(BaseCommand):
    help = "Fetch the connected QuickBooks company's CompanyInfo (read-only)."

    def handle(self, *args, **options):
        if not qbo.enabled():
            self.stderr.write(self.style.ERROR("Set QBO_CLIENT_ID and QBO_CLIENT_SECRET first."))
            raise SystemExit(1)
        try:
            info = qbo.company_info()
        except qbo.QboNotConnected as exc:
            self.stderr.write(
                self.style.ERROR(f"{exc} Connect it at /portal/settings/quickbooks/ first.")
            )
            raise SystemExit(1) from exc
        except qbo.QboAPIError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            raise SystemExit(1) from exc
        realm = qbo.QboCredential.load().realm_id
        self.stdout.write(self.style.SUCCESS("QuickBooks connection OK"))
        self.stdout.write(f"  environment: {settings.QBO_ENVIRONMENT}")
        self.stdout.write(f"  company id:  {realm}")
        self.stdout.write(f"  company:     {info.get('CompanyName', '?')}")
        if info.get("LegalName") and info.get("LegalName") != info.get("CompanyName"):
            self.stdout.write(f"  legal name:  {info['LegalName']}")
