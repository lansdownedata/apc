"""Seed weddings for the planner-portal checks (APC-66/67/68, checklist section 9).

Creates one planner contact (no PlannerAccount unless --account-password is given, so the
staff Invite flow can be run by hand) holding the Lead planner role on four orders:

  main     BOOKED, two days of trips starting --days-out days from today:
             day 1  Airport Transfer, Nashville BNA, America/Chicago (the "away" leg)
                    Hourly as directed (rehearsal), DC, DISPATCHED, driver Marcus Bell
             day 2  Wedding Transportation run of 2 vehicles (one linked group), DC; the
                    second has a confirmed driver but is only Affiliate is Assigned
                    Wedding Transportation getaway car, DC, 10:30 PM
           wedding_name left blank and no day-of coordinator, so "Waiting on you" opens
           (wedding names, day-of contact, final itinerary) via tasks.ensure_tasks.
  engaged  ENGAGED, one trip 60 days out -> "We're building your day".
  past     BOOKED, one Done trip 10 days ago -> "Past weddings", Archived.
  expired  QUOTED, first trip yesterday -> must NOT appear for the planner.

The couples have no email and no phone, so no customer message can go anywhere. Vehicles
are picked from types with no cleaning fee, outside the LARGE class (no cleaning hold).

Usage (repo root, venv active):
  python scripts/e2e/planner_wedding.py --dry-run
  python scripts/e2e/planner_wedding.py --planner-email moe+planner@lansdownedata.com
  python scripts/e2e/planner_wedding.py --only main --account-password 'pick-a-long-one-1'
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import time, timedelta
from decimal import Decimal

from _bootstrap import django_ready

NY, CHI = "America/New_York", "America/Chicago"
KINDS = ("main", "engaged", "past", "expired")


def parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed a planner contact + wedding orders for the planner-portal checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--planner-email",
        default="moe+planner@lansdownedata.com",
        help="planner contact's email (the sign-in name). Reused if a contact has it.",
    )
    p.add_argument("--planner-name", default="Priya Planner (E2E)")
    p.add_argument(
        "--agent-email",
        default="moe@lansdownedata.com",
        help="staff user set as the coordinator (assigned agent)",
    )
    p.add_argument(
        "--days-out",
        type=int,
        default=21,
        help="days from today to the main wedding's first day (default 21)",
    )
    p.add_argument(
        "--only",
        choices=KINDS,
        action="append",
        help="create only these orders (repeatable); default all four",
    )
    p.add_argument(
        "--account-password",
        default="",
        help="also create a REGISTERED planner account with this password "
        "(skips the invite email). Leave blank to invite from the UI.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything in a transaction, print it, then roll back",
    )
    return p.parse_args()


def main() -> None:
    args = parse()
    django_ready()

    from django.db import transaction
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.contacts.models import Contact
    from apps.dispatch.models import Assignment
    from apps.leads.models import Lead, LeadContact, ServiceType, VehicleType
    from apps.payments.models import PaymentPlan
    from apps.planners import services as planner_services
    from apps.planners.models import PlannerAccount
    from apps.reservations.models import Reservation, Stop
    from apps.tasks import services as task_services
    from apps.vendors.models import Vendor

    TS = Reservation.TripStatus
    wanted = set(args.only or KINDS)
    today = timezone.localdate()
    out: list[str] = []

    def vehicle(*names: str) -> VehicleType | None:
        ok = VehicleType.objects.filter(active=True, cleaning_fee=0).exclude(
            overtime_class=VehicleType.OvertimeClass.LARGE
        )
        for name in names:
            v = ok.filter(name=name).first()
            if v:
                return v
        return ok.order_by("-capacity").first()

    def service(name: str) -> ServiceType | None:
        return ServiceType.objects.filter(name=name).first()

    def trip(
        lead,
        *,
        day,
        at,
        zone,
        svc,
        veh,
        stops,
        kind=Reservation.TripType.TRANSFER,
        hours=1,
        status="",
        group=None,
        pax=4,
    ):
        t = Reservation.objects.create(
            lead=lead,
            vehicle=veh,
            trip_type=kind,
            service_type=service(svc),
            pickup_date=day,
            pickup_time=at,
            pickup_timezone=zone,
            passengers=pax,
            rate=Decimal("195.00"),
            hours=Decimal(hours),
            trip_status=status,
            group_key=group,
        )
        for i, (name, address) in enumerate(stops):
            Stop.objects.create(reservation=t, sequence=i, name=name, address=address)
        return t

    def driver(t, vendor, name, cell, desc, number):
        return Assignment.objects.create(
            reservation=t,
            vendor=vendor,
            status=Assignment.Status.CONFIRMED,
            payout=Decimal("140"),
            resolved_at=timezone.now(),
            affiliate_confirmed_at=timezone.now(),
            driver_name=name,
            driver_cell=cell,
            vehicle_desc=desc,
            vehicle_number=number,
            driver_info_at=timezone.now(),
        )

    def order(couple_name, status, intake, agent, planner):
        couple = Contact.objects.create(name=couple_name)
        lead = Lead.objects.create(
            contact=couple, status=status, assigned_agent=agent, intake_payload=intake
        )
        LeadContact.objects.create(lead=lead, contact=couple, role=LeadContact.Role.COUPLE)
        LeadContact.objects.create(lead=lead, contact=planner, role=LeadContact.Role.LEAD_PLANNER)
        return lead

    def plan(lead, deposit, balance):
        PaymentPlan.objects.create(
            lead=lead,
            deposit_pct=50,
            quote_total=lead.quote_total,
            deposit_status=deposit,
            balance_status=balance,
        )

    with transaction.atomic():
        agent = User.objects.filter(email__iexact=args.agent_email).first()
        email = args.planner_email.strip().lower()
        planner = Contact.objects.filter(email__iexact=email).first()
        if planner is None:
            planner = Contact.objects.create(name=args.planner_name, email=email)
            out.append(f"planner contact #{planner.pk} {planner.name} <{email}> (new)")
        else:
            out.append(f"planner contact #{planner.pk} {planner.name} <{email}> (reused)")

        vendor = Vendor.objects.filter(status=Vendor.Status.ACTIVE).order_by("name").first()
        if vendor is None:
            vendor = Vendor.objects.create(name="E2E Planner Livery")
        sedan = vehicle("Luxury Sedan")
        suv = vehicle("Luxury SUV")
        sprinter = vehicle("Sprinter Van")
        coach = vehicle("Mini Coach", "Minibus")

        if "main" in wanted:
            d1 = today + timedelta(days=args.days_out)
            d2 = d1 + timedelta(days=1)
            lead = order(
                "Harper & Lane (E2E)",
                Lead.Status.BOOKED,
                {
                    "wedding_date": d2.isoformat(),
                    "guest_count": 140,
                    "venue_name": "Anderson House",
                    "hotels": [{"name": "The Hay-Adams"}],
                },
                agent,
                planner,
            )
            trip(
                lead,
                day=d1,
                at=time(13, 30),
                zone=CHI,
                svc="Airport Transfer",
                veh=suv,
                stops=[
                    ("Nashville International Airport (BNA)", "1 Terminal Dr, Nashville, TN"),
                    ("Gaylord Opryland Resort", "2800 Opryland Dr, Nashville, TN"),
                ],
            )
            rehearsal = trip(
                lead,
                day=d1,
                at=time(18, 0),
                zone=NY,
                svc="Hourly as directed",
                veh=sprinter,
                kind=Reservation.TripType.HOURLY,
                hours=4,
                status=TS.DISPATCHED,
                pax=12,
                stops=[
                    ("The Hay-Adams", "800 16th St NW, Washington, DC"),
                    ("Old Ebbitt Grill", "675 15th St NW, Washington, DC"),
                    ("The Hay-Adams", "800 16th St NW, Washington, DC"),
                ],
            )
            driver(
                rehearsal, vendor, "Marcus Bell", "+12025550142", "Black Mercedes Sprinter", "12"
            )
            run = uuid.uuid4()
            trip(
                lead,
                day=d2,
                at=time(14, 30),
                zone=NY,
                svc="Wedding Transportation",
                veh=sprinter,
                group=run,
                pax=14,
                stops=[
                    ("The Hay-Adams", "800 16th St NW, Washington, DC"),
                    ("St. Matthew's Cathedral", "1725 Rhode Island Ave NW, Washington, DC"),
                ],
            )
            second = trip(
                lead,
                day=d2,
                at=time(14, 30),
                zone=NY,
                svc="Wedding Transportation",
                veh=coach,
                group=run,
                status=TS.AFFILIATE_ASSIGNED,
                pax=20,
                stops=[
                    ("The Hay-Adams", "800 16th St NW, Washington, DC"),
                    ("St. Matthew's Cathedral", "1725 Rhode Island Ave NW, Washington, DC"),
                ],
            )
            driver(second, vendor, "Dana Ortiz", "+12025550177", "White Mini Coach", "31")
            trip(
                lead,
                day=d2,
                at=time(22, 30),
                zone=NY,
                svc="Wedding Transportation",
                veh=sedan,
                pax=2,
                stops=[
                    ("Anderson House", "2118 Massachusetts Ave NW, Washington, DC"),
                    ("The Hay-Adams", "800 16th St NW, Washington, DC"),
                ],
            )
            plan(lead, PaymentPlan.DepositStatus.PAID, PaymentPlan.BalanceStatus.SCHEDULED)
            tasks = task_services.ensure_tasks(lead)
            waiting = [
                t.kind
                for t in tasks
                if t.kind in ("wedding_names", "day_of_contact", "final_itinerary")
            ]
            out.append(
                f"main    {lead.quote_no} lead #{lead.pk} BOOKED, days {d1} / {d2}; "
                f"rehearsal #{rehearsal.pk} dispatched, coach #{second.pk} driver "
                f"hidden until Dispatched; tasks opened {len(tasks)} "
                f"(waiting on planner: {', '.join(waiting) or 'none'})"
            )

        if "engaged" in wanted:
            day = today + timedelta(days=60)
            lead = order(
                "Rivera & Chen (E2E)",
                Lead.Status.ENGAGED,
                {
                    "wedding_date": day.isoformat(),
                    "guest_count": 90,
                    "venue_name": "Dumbarton House",
                    "ceremony": {"name": "Dumbarton House garden"},
                    "hotels": [{"name": "The Georgetown Inn"}],
                },
                agent,
                planner,
            )
            trip(
                lead,
                day=day,
                at=time(16, 0),
                zone=NY,
                svc="Wedding Transportation",
                veh=sprinter,
                stops=[
                    ("The Georgetown Inn", "1310 Wisconsin Ave NW"),
                    ("Dumbarton House", "2715 Q St NW"),
                ],
            )
            plan(lead, PaymentPlan.DepositStatus.AUTHORIZED, PaymentPlan.BalanceStatus.NA)
            out.append(f"engaged {lead.quote_no} lead #{lead.pk} ENGAGED, {day}")

        if "past" in wanted:
            day = today - timedelta(days=10)
            lead = order(
                "Morgan & Ellis (E2E)",
                Lead.Status.BOOKED,
                {"wedding_date": day.isoformat(), "guest_count": 60},
                agent,
                planner,
            )
            trip(
                lead,
                day=day,
                at=time(16, 0),
                zone=NY,
                svc="Wedding Transportation",
                veh=sedan,
                status=TS.DONE,
                stops=[("The Jefferson", "1200 16th St NW"), ("Tabard Inn", "1739 N St NW")],
            )
            plan(lead, PaymentPlan.DepositStatus.PAID, PaymentPlan.BalanceStatus.PAID)
            out.append(f"past    {lead.quote_no} lead #{lead.pk} BOOKED, Done trip on {day}")

        if "expired" in wanted:
            day = today - timedelta(days=1)
            lead = order(
                "Patel & Brooks (E2E)",
                Lead.Status.QUOTED,
                {"wedding_date": day.isoformat()},
                agent,
                planner,
            )
            trip(
                lead,
                day=day,
                at=time(15, 0),
                zone=NY,
                svc="Wedding Transportation",
                veh=sedan,
                stops=[
                    ("Willard InterContinental", "1401 Pennsylvania Ave NW"),
                    ("National Cathedral", "3101 Wisconsin Ave NW"),
                ],
            )
            out.append(
                f"expired {lead.quote_no} lead #{lead.pk} QUOTED, first trip {day} (must not show)"
            )

        if args.account_password:
            account = PlannerAccount.objects.filter(contact=planner).first()
            if account is None:
                account = PlannerAccount(
                    contact=planner, email=email, invited_at=timezone.now(), invited_by=agent
                )
                account.set_unusable_password()
                account.save()
            if account.revoked_at:
                account.revoked_at = None
                account.save(update_fields=["revoked_at"])
            planner_services.register(account, args.account_password)
            out.append(f"planner account #{account.pk} registered (no email sent)")

        visible = planner_services.visible_leads(planner.pk).count()
        out.append(f"planner sees {visible} wedding(s) now (expired quote and lost excluded)")

        if args.dry_run:
            transaction.set_rollback(True)

    print("DRY RUN, rolled back. Would have created:" if args.dry_run else "Created:")
    for line in out:
        print("  " + line)
    if not args.dry_run:
        print(f"Staff: /portal/contacts/{planner.pk}/  ·  Planner: /planner/")


if __name__ == "__main__":
    sys.exit(main())
