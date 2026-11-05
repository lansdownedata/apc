"""Seed the six staff report rows, each with its staff emails from where its list lived
before APC-120 (spec C section 7 step 4):

- the unpaid-deposit and unconfirmed-trips reports: the `DEPOSIT_REPORT_EMAILS` and
  `TRIP_CONFIRM_REPORT_EMAILS` env settings, read from the environment as `env.list` read
  them (unset means the company email; set but empty means nobody, as it did), each
  address trimmed;
- the overdue-task digest, the review and the complaint reports: the Tasks screen's digest
  and report lists (blank report recipients used the digest's, a blank digest the company
  email);
- the dispatch alert: the Dispatch alerts screen's email list (blank: the company email).

It runs before the Tasks and Dispatch alerts migrations that drop those fields, and reads
them through the historical models. The env settings are gone from the code by now, so the
environment must still carry them when this runs.
"""

import os
import re

from django.conf import settings
from django.db import migrations

# The report messages this migration seeds, frozen: later seed migrations own the keys they
# add (see 0022).
KEYS = (
    "deposit_report",
    "review_report",
    "complaint_report",
    "unconfirmed_trips_report",
    "task_digest",
    "dispatch_alert",
)


def _split(raw: str | None) -> list[str]:
    """The screens' parsing: comma, newline or semicolon separated, order kept."""
    return [part.strip() for part in re.split(r"[,\n;]+", raw or "") if part.strip()]


def _env_list(raw: str | None, fallback: list[str]) -> list[str]:
    """An env list as `env.list` read it (unset is the fallback, set but empty is nobody),
    each address trimmed."""
    if raw is None:
        return list(fallback)
    return [part.strip() for part in raw.split(",") if part.strip()]


def staff_lists(
    environ, *, digest_emails: str, report_emails: str, alert_emails: str, company_email: str
) -> dict[str, list[str]]:
    """Each report's staff emails, from where its list lived."""
    company = [company_email] if company_email else []
    digest = _split(digest_emails) or company
    reports = _split(report_emails) or digest
    return {
        "deposit_report": _env_list(environ.get("DEPOSIT_REPORT_EMAILS"), company),
        "unconfirmed_trips_report": _env_list(environ.get("TRIP_CONFIRM_REPORT_EMAILS"), company),
        "review_report": reports,
        "complaint_report": list(reports),
        "task_digest": digest,
        "dispatch_alert": _split(alert_emails) or company,
    }


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    already = set(
        MessageTemplate.objects.filter(key__in=KEYS, seeded=True).values_list("key", flat=True)
    )
    defaults.seed(apps, keys=KEYS)
    tasks = apps.get_model("tasks", "TaskConfig").objects.filter(pk=1).first()
    alerts = apps.get_model("dispatch", "DispatchAlertConfig").objects.filter(pk=1).first()
    lists = staff_lists(
        os.environ,
        digest_emails=tasks.digest_emails if tasks else "",
        report_emails=tasks.report_emails if tasks else "",
        alert_emails=alerts.alert_emails if alerts else "",
        company_email=settings.COMPANY_EMAIL,
    )
    for key in KEYS:
        if key not in already:
            MessageTemplate.objects.filter(key=key).update(staff_emails=lists[key])


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0027_seed_invite_rows"),
        ("tasks", "0015_driver_released_follows_message"),
        ("dispatch", "0011_required_driver_fields"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
