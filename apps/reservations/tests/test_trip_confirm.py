"""APC-95 — the order-keyed confirmation for every held order (spec 2026-10-27 §3).

A booked card order with a trip in a vehicle that carries a cleaning fee is confirmed the
way a wedding is: through the order's own link, sent to the billing contact, with the
per-type fee block and both boxes required. Old wedding links keep opening.
"""

from datetime import time, timedelta
from decimal import Decimal
from itertools import count

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.messaging import rules
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.recipients import resolve
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db

BASE = "https://apc.example.com"
ALL_BOXES = {"ack": "on", "ack_cleaning": "on", "ack_overtime": "on"}
_types = count()


@pytest.fixture(autouse=True)
def _base_url(settings):
    settings.PUBLIC_BASE_URL = BASE


def _order(fee="250", **lead_kw):
    lead_kw.setdefault("status", Lead.Status.BOOKED)
    lead = LeadFactory(**lead_kw)
    vehicle = VehicleTypeFactory(name=f"Sprinter {next(_types)}", cleaning_fee=Decimal(fee))
    trip = ReservationFactory(
        lead=lead,
        vehicle=vehicle,
        pickup_date=timezone.localdate() + timedelta(days=3),
        pickup_time=time(14, 0),
        pickup_timezone="America/New_York",
    )
    return lead, trip


def _tp(key, lead, trip):
    row = MessageTemplate.objects.get(key=key)
    tp = TouchPointFactory(
        lead=lead, reservation=trip, kind=key, message=row, scheduled_for=timezone.now()
    )
    return TouchPoint.objects.select_related("lead", "reservation").get(pk=tp.pk), row


def _order_url(lead, day):
    return reverse("order_trip_confirm", args=[ack.make_order_day_ack_token(lead, day)])


@pytest.mark.parametrize("key", ["trip_confirm_customer", "trip_confirm_customer_2"])
def test_held_non_wedding_order_gets_the_order_keyed_link_to_the_billing_contact(key):
    payer = ContactFactory(name="Pat Okafor", phone="+12025550103")
    lead, trip = _order(contact=ContactFactory(name="Casey Morgan"))
    lead.billing_contact = payer
    lead.save()
    tp, row = _tp(key, lead, trip)

    [to] = rules.recipients_for(tp, row, resolve(row, lead, trip))
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)

    assert (to.contact_id, to.label) == (payer.pk, "Pat Okafor (billing contact)")
    assert ctx["confirm_link"] == ack.order_day_ack_url(lead, trip.pickup_date, base_url=BASE)
    assert "/trip/o/" in ctx["confirm_link"]
    # Keyed on the order, never merged with the booker's other orders that day.
    assert rules.combine_key(tp, row) == f"{trip.pickup_date.isoformat()}:wedding:{lead.pk}"


def test_unheld_non_wedding_order_keeps_the_per_contact_link():
    lead, trip = _order(fee="0")
    tp, row = _tp("trip_confirm_customer", lead, trip)

    [to] = rules.recipients_for(tp, row, resolve(row, lead, trip))
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)

    assert to.contact_id == lead.contact_id
    assert ctx["confirm_link"] == ack.trip_day_ack_url(
        lead.contact, trip.pickup_date, base_url=BASE
    )
    assert rules.combine_key(tp, row) == trip.pickup_date.isoformat()


def test_a_held_order_on_account_keeps_the_per_contact_link():
    """Spec §2: an order billed to an account is never held."""
    from apps.billing import services as billing

    lead, trip = _order()
    group = billing.create_account(
        lead.contact, name="Ridgeline", group_name="AP", invoice_email="ap@x.example"
    ).groups.get()
    lead.billing_group = group
    lead.save()
    tp, row = _tp("trip_confirm_customer", lead, trip)
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)

    assert "/trip/o/" not in ctx["confirm_link"]


def test_the_held_order_and_the_bookers_plain_order_stay_apart():
    held, held_trip = _order()
    plain = LeadFactory(status=Lead.Status.BOOKED, contact=held.contact)
    plain_trip = ReservationFactory(
        lead=plain, pickup_date=held_trip.pickup_date, pickup_time=time(18, 0)
    )

    held_tp, _row = _tp("trip_confirm_customer", held, held_trip)
    plain_tp, _row = _tp("trip_confirm_customer", plain, plain_trip)

    assert [t.pk for t in rules.confirm_group(held_tp)] == [held_trip.pk]
    assert [t.pk for t in rules.confirm_group(plain_tp)] == [plain_trip.pk]


def test_a_held_day_is_not_confirmed_until_the_fees_are_acknowledged():
    lead, trip = _order()
    trip.customer_confirmed_at = timezone.now()
    trip.save(update_fields=["customer_confirmed_at"])
    tp, row = _tp("trip_confirm_customer_2", lead, trip)

    assert rules.check(tp, row)[0] == "send"


def test_order_confirm_requires_both_boxes_when_held(client):
    lead, trip = _order()
    url = _order_url(lead, trip.pickup_date)

    body = client.get(url).content.decode()
    assert "Before you confirm" in body
    assert "$250.00" in body
    assert 'name="ack_cleaning"' in body and 'name="ack_overtime"' in body

    refused = client.post(url, {"ack": "on", "ack_overtime": "on"})
    assert refused.status_code == 200
    assert "acknowledge the cleaning fee and the overtime" in refused.content.decode()
    trip.refresh_from_db()
    lead.refresh_from_db()
    assert trip.customer_confirmed_at is None and lead.fees_acknowledged_at is None

    done = client.post(url, ALL_BOXES)
    assert done.status_code == 302
    trip.refresh_from_db()
    lead.refresh_from_db()
    assert trip.customer_confirmed_at is not None
    assert lead.fees_acknowledged_at is not None
    assert "Cleaning fee and overtime acknowledged on" in client.get(url).content.decode()


def test_the_held_confirm_page_mentions_the_hold_while_holds_are_on(client):
    lead, trip = _order()
    cfg = TaskConfig.load()
    cfg.hold_enabled = True
    cfg.save()

    body = client.get(_order_url(lead, trip.pickup_date)).content.decode()

    assert "and a hold of $250.00 is placed on my card" in body
    assert "One hold for the highest of these, $250.00" in body


def test_old_wedding_link_still_opens(client):
    lead, trip = _order()
    token = ack.make_wedding_day_ack_token(lead, trip.pickup_date)

    old = reverse("wedding_trip_confirm", args=[token])
    assert old.startswith("/trip/w/")
    assert client.get(old).status_code == 200
    # The renamed helpers are the same token family.
    day = trip.pickup_date
    assert ack.wedding_day_ack_url(lead, day) == ack.order_day_ack_url(lead, day)
    assert ack.read_order_day_ack_token(token) == (lead, trip.pickup_date)
    assert client.get(_order_url(lead, trip.pickup_date)).status_code == 200
