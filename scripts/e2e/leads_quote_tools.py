"""Quote helpers for the Leads & Quotes checks (LEAD-*): print a lead's customer links, and
move a quote's expiry so the expired / expiring states can be tested without waiting.

`links` is read-only. It prints every token-keyed customer URL for one lead, on both the
local host and PUBLIC_BASE_URL (the ngrok host the emails use): the quote page, the pay
page, the terms page, a wedding's resume link, and each wedding trip's day-of details
link (the T-7d form, APC-18). The tokens are signed, so these are the real links.

`expiry` rewrites `Lead.quote_expires_at` on a QUOTED lead:
  --expired           expires_at = now - 1 minute (or --ago-hours N)
  --in-hours H        expires_at = now + H hours (24.05 puts the "Quote expiring" nudge
                      about 3 minutes out, if that message row is on)
  --recompute         expires_at = leads.services.compute_quote_expiry(lead), the real rule
                      (pickup - 14 days; inside 14 days, the pickup itself; no trip date,
                      now + 14 days)
Moving the expiry does NOT move the expiring/expired nudges already queued from the old
expiry. Add --requeue-nudges to withdraw them and queue them afresh from the new one
(events.cancel(..., triggers=["quote_expiry"]) then events.schedule_dates(send_inline=False);
anything already due goes out on the next run-touchpoints cron, not inline).

Examples (repo root, venv active):
  python scripts/e2e/leads_quote_tools.py links 68
  python scripts/e2e/leads_quote_tools.py expiry 68 --expired --dry-run
  python scripts/e2e/leads_quote_tools.py expiry 68 --in-hours 24.05 --requeue-nudges
  python scripts/e2e/leads_quote_tools.py expiry 68 --recompute
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from _bootstrap import BASE_URL, django_ready, env


class _Rollback(Exception):
    pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Print a lead's customer links, or move its quote expiry (LEAD-* checks).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples" + __doc__.split("Examples", 1)[1],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    links = sub.add_parser("links", help="print the lead's token-keyed customer URLs (read-only)")
    links.add_argument("lead", type=int, help="Lead pk (the quote number minus 100000)")

    exp = sub.add_parser("expiry", help="set the quote's expiry (QUOTED leads only)")
    exp.add_argument("lead", type=int, help="Lead pk (the quote number minus 100000)")
    when = exp.add_mutually_exclusive_group(required=True)
    when.add_argument("--expired", action="store_true", help="expire it now")
    when.add_argument("--in-hours", type=float, metavar="H", help="expire H hours from now")
    when.add_argument(
        "--recompute", action="store_true", help="reset to the real rule (compute_quote_expiry)"
    )
    exp.add_argument(
        "--ago-hours",
        type=float,
        default=1 / 60,
        metavar="N",
        help="with --expired: how long ago it expired (default 1 minute)",
    )
    exp.add_argument(
        "--requeue-nudges",
        action="store_true",
        help="withdraw queued expiring/expired nudges and queue them from the new expiry",
    )
    exp.add_argument("--dry-run", action="store_true", help="do it in a transaction, roll back")
    return p.parse_args()


def get_lead(pk: int):
    from apps.leads.models import Lead

    lead = Lead.objects.select_related("contact").filter(pk=pk).first()
    if lead is None:
        sys.exit(f"No lead with pk={pk}.")
    return lead


def cmd_links(pk: int) -> None:
    from django.urls import reverse

    from apps.leads import services
    from apps.public.services import make_wedding_token
    from apps.public.wedding import is_wedding_payload
    from apps.reservations import acknowledgements as ack
    from apps.reservations.services import is_wedding_trip

    lead = get_lead(pk)
    public = env("PUBLIC_BASE_URL").rstrip("/")
    bases = [("local", BASE_URL.rstrip("/"))] + ([("public", public)] if public else [])
    print(f"{lead.quote_no} · {lead.contact.name} · status {lead.status}")
    print(f"  quote_expires_at {lead.quote_expires_at} · expired={lead.quote_expired}")
    for label, base in bases:
        print(f"[{label}]")
        print(f"  quote  {services.make_quote_page_url(lead, base_url=base)}")
        print(f"  pay    {services.make_pay_page_url(lead, base_url=base)}")
        print(f"  terms  {services.make_terms_page_url(lead, base_url=base)}")
        if is_wedding_payload(lead.intake_payload or {}):
            token = make_wedding_token(lead)
            print(f"  wedding resume  {base}{reverse('public:wedding_resume', args=[token])}")
        for res in lead.reservations.order_by("pickup_date", "pickup_time", "pk"):
            if not is_wedding_trip(res):
                continue
            print(f"  trip #{res.pk} day-of details  {ack.wedding_details_url(res, base_url=base)}")
    if lead.status not in (lead.Status.QUOTED, lead.Status.BOOKED):
        print("  note: the quote page 404s unless the lead is Quoted or Booked.")


def cmd_expiry(args) -> None:
    from django.db import transaction
    from django.utils import timezone

    from apps.leads import services
    from apps.messaging import events
    from apps.messaging.models import TouchPoint

    try:
        with transaction.atomic():
            lead = get_lead(args.lead)
            if lead.status != lead.Status.QUOTED:
                sys.exit(f"{lead.quote_no} is {lead.status}; only a Quoted lead can expire.")
            before = lead.quote_expires_at
            now = timezone.now()
            if args.expired:
                lead.quote_expires_at = now - timedelta(hours=args.ago_hours)
            elif args.in_hours is not None:
                lead.quote_expires_at = now + timedelta(hours=args.in_hours)
            else:
                lead.quote_expires_at = services.compute_quote_expiry(lead)
            lead.save(update_fields=["quote_expires_at", "updated_at"])
            print(f"{lead.quote_no}: quote_expires_at {before} -> {lead.quote_expires_at}")
            print(f"  quote_expired now {lead.quote_expired}")
            if args.requeue_nudges:
                withdrawn = events.cancel(lead, triggers=["quote_expiry"])
                events.schedule_dates(lead, send_inline=False)
                print(
                    f"  withdrew {withdrawn} queued expiry nudge(s); requeued from the new expiry"
                )
            rows = TouchPoint.objects.filter(lead=lead, kind__in=["tp7_expiring", "tp8_expired"])
            for tp in rows.order_by("pk"):
                print(f"  TouchPoint #{tp.pk} {tp.kind} {tp.status} at {tp.scheduled_for}")
            if args.dry_run:
                raise _Rollback
    except _Rollback:
        print("dry run: rolled back, nothing saved.")


def main() -> None:
    args = parse_args()
    django_ready()
    if args.cmd == "links":
        cmd_links(args.lead)
    else:
        cmd_expiry(args)


if __name__ == "__main__":
    main()
