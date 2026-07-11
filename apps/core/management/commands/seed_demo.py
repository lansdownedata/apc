"""Seed realistic demo data so the portal screens have something to show.

python manage.py seed_demo            # seed once (skips if leads exist)
python manage.py seed_demo --fresh    # wipe demo data and reseed
"""

from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.leads.models import Lead, Vehicle
from apps.messaging.models import Message
from apps.notifications.models import Notification
from apps.payments.models import Charge, PaymentPlan
from apps.reservations.models import Reservation, Stop

User = get_user_model()


class Command(BaseCommand):
    help = "Seed demo contacts, vehicles, leads, reservations, payments, and notifications."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fresh", action="store_true", help="Wipe existing demo data before seeding."
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts["fresh"]:
            for model in (
                Notification,
                Message,
                Charge,
                PaymentPlan,
                Stop,
                Reservation,
                Lead,
                Contact,
            ):
                model.objects.all().delete()
            self.stdout.write("Wiped existing demo data.")
        elif Lead.objects.exists():
            self.stdout.write(
                self.style.WARNING("Leads already exist — pass --fresh to reseed. Skipping.")
            )
            return

        agent = User.objects.filter(is_superuser=True).order_by("id").first()
        today = date.today()

        vehicles = {
            name: Vehicle.objects.create(name=name, capacity=cap, klass=klass)
            for name, cap, klass in [
                ("Executive Sedan", 3, Vehicle.Klass.SEDAN),
                ("Luxury SUV", 6, Vehicle.Klass.SUV),
                ("Sprinter Van", 12, Vehicle.Klass.VAN),
                ("Mini Coach", 24, Vehicle.Klass.MINI_COACH),
            ]
        }

        def new_lead(name, company, phone, email, channel, status, notes=""):
            contact = Contact.objects.create(
                name=name, company=company, phone=phone, email=email, channel=channel
            )
            return Lead.objects.create(
                contact=contact,
                assigned_agent=agent,
                channel=channel,
                status=status,
                notes=notes,
            )

        def transfer(lead, service, vehicle, days_out, t, pax, rate, stops, status=""):
            res = Reservation.objects.create(
                lead=lead,
                vehicle=vehicles[vehicle],
                trip_type=Reservation.TripType.TRANSFER,
                service=service,
                pickup_date=today + timedelta(days=days_out),
                pickup_time=t,
                passengers=pax,
                base_rate=Decimal(rate),
                trip_status=status,
            )
            for i, addr in enumerate(stops):
                Stop.objects.create(reservation=res, sequence=i, address=addr)
            return res

        def hourly(lead, service, vehicle, days_out, t, pax, rate, hrs, minimum, stops, status=""):
            res = Reservation.objects.create(
                lead=lead,
                vehicle=vehicles[vehicle],
                trip_type=Reservation.TripType.HOURLY,
                service=service,
                pickup_date=today + timedelta(days=days_out),
                pickup_time=t,
                passengers=pax,
                hourly_rate=Decimal(rate),
                hours=Decimal(hrs),
                min_hours=Decimal(minimum),
                trip_status=status,
            )
            for i, addr in enumerate(stops):
                Stop.objects.create(reservation=res, sequence=i, address=addr)
            return res

        def plan(lead, deposit_status, balance_status, **extra):
            p = PaymentPlan.objects.create(
                lead=lead,
                deposit_pct=50,
                quote_total=lead.quote_total,
                deposit_status=deposit_status,
                balance_status=balance_status,
                **extra,
            )
            return p

        def message(lead, direction, body, channel=Message.Channel.SMS, unread=False):
            """A Podium conversation message. `unread` only applies to inbound messages."""
            return Message.objects.create(
                lead=lead,
                direction=direction,
                channel=channel,
                body=body,
                sent_at=timezone.now() if direction == Message.Direction.OUT else None,
                read_at=None if (unread and direction == Message.Direction.IN) else timezone.now(),
                delivery_status=Message.DeliveryStatus.SENT
                if direction == Message.Direction.OUT
                else Message.DeliveryStatus.RECEIVED,
            )

        TS = Reservation.TripStatus

        # 1) NEW — website — single airport transfer, no quote sent yet.
        lead1 = new_lead(
            "Marcus Halloway",
            "",
            "(617) 555-0142",
            "marcus.h@gmail.com",
            Channel.WEBSITE,
            Lead.Status.NEW,
            notes="Inbound from website form. Wants a quote for an airport run.",
        )
        transfer(
            lead1,
            "Airport transfer — BOS arrivals",
            "Executive Sedan",
            21,
            time(14, 30),
            2,
            165,
            ["Boston Logan Intl (BOS), Terminal E", "The Newbury, 15 Arlington St, Boston"],
        )
        message(
            lead1,
            Message.Direction.IN,
            "Hi, I filled out the form for an airport pickup on the 25th — can you send a quote?",
            unread=True,
        )

        # 2) QUOTED — wedding pro — hourly as-directed + a transfer, deposit requested.
        lead2 = new_lead(
            "Priya & Daniel Wedding",
            "Eventful Co.",
            "(401) 555-0199",
            "events@eventful.co",
            Channel.WEDDING_PRO,
            Lead.Status.QUOTED,
            notes="Wedding shuttle + couple's car. Deposit request sent.",
        )
        hourly(
            lead2,
            "As-directed — wedding party",
            "Mini Coach",
            40,
            time(15, 0),
            22,
            195,
            6,
            5,
            ["Omni Providence Hotel", "Roger Williams Park", "Omni Providence Hotel"],
        )
        transfer(
            lead2,
            "Couple's getaway car",
            "Luxury SUV",
            40,
            time(22, 30),
            2,
            240,
            ["Roger Williams Park Casino", "Providence Biltmore"],
        )
        plan(lead2, PaymentPlan.DepositStatus.REQUESTED, PaymentPlan.BalanceStatus.NA)
        message(
            lead2,
            Message.Direction.OUT,
            "Hi Priya! Here's your quote for the wedding shuttle + getaway car.",
        )
        message(
            lead2,
            Message.Direction.IN,
            "This looks great — sending the deposit link to Daniel now.",
        )
        message(
            lead2,
            Message.Direction.IN,
            "One more question — can the SUV wait outside the reception until 11pm?",
            unread=True,
        )

        # 3) BOOKED — phone — multi-stop wine tour, deposit paid, balance scheduled.
        lead3 = new_lead(
            "Eleanor Whitfield",
            "Whitfield Estate",
            "(774) 555-0121",
            "ew@whitfield.com",
            Channel.PHONE,
            Lead.Status.BOOKED,
            notes="Annual wine country tour. Repeat client.",
        )
        hourly(
            lead3,
            "Napa wine tour — as directed",
            "Sprinter Van",
            12,
            time(10, 0),
            8,
            165,
            7,
            6,
            [
                "Fairmont Sonoma Mission Inn",
                "Domaine Carneros",
                "Castello di Amorosa",
                "Frog's Leap Winery",
                "Fairmont Sonoma Mission Inn",
            ],
            status=TS.ASSIGNED,
        )
        p3 = plan(
            lead3,
            PaymentPlan.DepositStatus.PAID,
            PaymentPlan.BalanceStatus.SCHEDULED,
            stripe_customer_id="cus_demo3",
            stripe_payment_method_id="pm_demo3",
            card_brand="visa",
            card_last4="4242",
        )
        Charge.objects.create(
            plan=p3,
            kind=Charge.Kind.DEPOSIT,
            amount=p3.deposit_amount,
            status=Charge.Status.SUCCEEDED,
            idempotency_key="seed-plan3-deposit-1",
        )
        message(
            lead3,
            Message.Direction.OUT,
            "Thanks for booking again this year, Eleanor! Deposit received — you're all set.",
            channel=Message.Channel.EMAIL,
        )
        message(lead3, Message.Direction.IN, "Wonderful, thank you! Same pickup spot as last year.")

        # 4) BOOKED — api — corporate roadshow, deposit paid, BALANCE FAILED (alert).
        lead4 = new_lead(
            "Theo Nakamura",
            "Lansdowne Capital",
            "(212) 555-0177",
            "tnakamura@lansdownecap.com",
            Channel.API,
            Lead.Status.BOOKED,
            notes="Two-day investor roadshow. Card on file declined for the balance.",
        )
        transfer(
            lead4,
            "Investor roadshow — day 1",
            "Executive Sedan",
            18,
            time(8, 0),
            3,
            320,
            ["The Langham, Boston", "Lansdowne Capital HQ, 200 Clarendon St"],
            status=TS.DONE,
        )
        transfer(
            lead4,
            "Investor roadshow — day 2",
            "Luxury SUV",
            19,
            time(8, 0),
            4,
            380,
            ["The Langham, Boston", "Cambridge Innovation Center"],
            status=TS.ASSIGNED,
        )
        p4 = plan(
            lead4,
            PaymentPlan.DepositStatus.PAID,
            PaymentPlan.BalanceStatus.FAILED,
            stripe_customer_id="cus_demo4",
            stripe_payment_method_id="pm_demo4",
            card_brand="mastercard",
            card_last4="0341",
            fail_reason="Your card was declined.",
        )
        Charge.objects.create(
            plan=p4,
            kind=Charge.Kind.DEPOSIT,
            amount=p4.deposit_amount,
            status=Charge.Status.SUCCEEDED,
            idempotency_key="seed-plan4-deposit-1",
        )
        Charge.objects.create(
            plan=p4,
            kind=Charge.Kind.BALANCE,
            amount=p4.balance_amount,
            status=Charge.Status.FAILED,
            failure_reason="Your card was declined.",
            idempotency_key="seed-plan4-balance-1",
        )
        lead4.has_alert = True
        lead4.save(update_fields=["has_alert", "updated_at"])
        Notification.notify(
            lead4,
            Notification.Kind.BALANCE_FAILED,
            title="Balance charge failed",
            detail="Theo Nakamura · Your card was declined.",
            user=agent,
        )

        # 5) QUOTED — website — prom night stretch, deposit requested.
        lead5 = new_lead(
            "Sofia Reyes",
            "",
            "(508) 555-0163",
            "sofia.reyes@icloud.com",
            Channel.WEBSITE,
            Lead.Status.QUOTED,
            notes="Prom night — 5 hour package.",
        )
        hourly(
            lead5,
            "Prom night package",
            "Mini Coach",
            33,
            time(17, 30),
            18,
            175,
            5,
            5,
            ["Newton North High School", "Downtown Boston", "Newton North High School"],
        )
        plan(lead5, PaymentPlan.DepositStatus.REQUESTED, PaymentPlan.BalanceStatus.NA)

        # 6) NEW — phone — quick quote request.
        lead6 = new_lead(
            "Grant McAllister",
            "McAllister & Co.",
            "(617) 555-0188",
            "grant@mcallisterco.com",
            Channel.PHONE,
            Lead.Status.NEW,
            notes="Requested a quote for a board dinner transfer.",
        )
        transfer(
            lead6,
            "Board dinner — round trip",
            "Luxury SUV",
            9,
            time(18, 45),
            6,
            295,
            ["Four Seasons Boston", "Grill 23 & Bar", "Four Seasons Boston"],
        )

        # 7) LOST — wedding pro — went with another vendor.
        lead7 = new_lead(
            "Hannah & Co.",
            "Bliss Weddings",
            "(781) 555-0150",
            "hannah@blissweddings.com",
            Channel.WEDDING_PRO,
            Lead.Status.LOST,
            notes="Chose a competitor on price.",
        )
        lead7.lost_reason = "Price"
        lead7.save(update_fields=["lost_reason", "updated_at"])
        transfer(
            lead7,
            "Wedding guest shuttle",
            "Sprinter Van",
            60,
            time(16, 0),
            12,
            410,
            ["Hotel Viking, Newport", "Castle Hill Inn"],
        )

        leads = Lead.objects.count()
        res = Reservation.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {leads} leads, {res} reservations, "
                f"{PaymentPlan.objects.count()} payment plans, "
                f"{Notification.objects.count()} notification(s)."
            )
        )
