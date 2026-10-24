"""Public token-keyed acknowledgement pages (APC-18 / APC-19 / APC-20)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db


def _future(days=20):
    return (timezone.now() + timedelta(days=days)).date()


# --- customer trip confirmation (APC-19) --------------------------------------------
# The link covers a customer's whole day, so these build through `_day_url`.


def _day_url(res):
    return reverse(
        "trip_confirm", args=[ack.make_trip_day_ack_token(res.lead.contact, res.pickup_date)]
    )


def test_trip_confirm_get_then_post_stamps_confirmed_at(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    url = _day_url(res)

    assert client.get(url).status_code == 200
    resp = client.post(url, {"ack": "on"})
    assert resp.status_code == 302

    res.refresh_from_db()
    assert res.customer_confirmed_at is not None


def test_trip_confirm_is_idempotent(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    url = _day_url(res)
    client.post(url, {"ack": "on"})
    first = ReservationFactory._meta.model.objects.get(pk=res.pk).customer_confirmed_at
    client.post(url, {"ack": "on"})
    res.refresh_from_db()
    assert res.customer_confirmed_at == first


def test_trip_confirm_bad_token_404s(client):
    assert client.get(reverse("trip_confirm", args=["not-a-real-token"])).status_code == 404


def test_trip_confirm_confirms_every_trip_that_day_from_one_checkbox(client):
    contact = ContactFactory()
    day = _future()
    first = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED), pickup_date=day
    )
    second = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED), pickup_date=day
    )

    client.post(_day_url(first), {"ack": "on"})

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.customer_confirmed_at is not None
    assert second.customer_confirmed_at is not None


def test_trip_confirm_page_lists_every_trip_and_names_the_contact(client):
    contact = ContactFactory(name="Dana Whitfield")
    day = _future()
    morning = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED),
        pickup_date=day,
        stops=["Dulles International", "The Willard"],
    )
    ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED),
        pickup_date=day,
        stops=["The Willard", "Reagan National"],
    )

    body = client.get(_day_url(morning)).content.decode()

    assert "Dana Whitfield" in body
    assert "Dulles International" in body
    assert "Reagan National" in body


def test_trip_confirm_requires_the_acknowledgement_box(client):
    """`required` is client-side only — an unchecked POST must not confirm."""
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())

    resp = client.post(_day_url(res), {})

    assert resp.status_code == 200
    res.refresh_from_db()
    assert res.customer_confirmed_at is None


def test_trip_confirm_never_shows_driver_or_affiliate_detail(client):
    """Trip details only — no farm-out company, no driver, no payout (client rule)."""
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment

    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED, payout=250)

    body = client.get(_day_url(res)).content.decode()

    assert a.vendor.name not in body
    assert "Payout" not in body


def test_trip_confirm_shows_nothing_to_confirm_when_the_day_is_cancelled(client):
    res = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future(), trip_status="cancelled"
    )
    url = _day_url(res)
    resp = client.get(url)
    assert resp.status_code == 200
    assert b"cancelled" in resp.content.lower()
    client.post(url, {"ack": "on"})
    res.refresh_from_db()
    assert res.customer_confirmed_at is None


# --- wedding: cleaning fee + overtime acknowledgment (APC-94) ----------------------------


def _fee(amount):
    cfg = TaskConfig.load()
    cfg.cleaning_fee = Decimal(amount)
    cfg.save()
    return cfg


def _wedding_res(**kw):
    kw.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    return ReservationFactory(
        service_type=ServiceTypeFactory(name="Wedding Transportation"), pickup_date=_future(), **kw
    )


ALL_BOXES = {"ack": "on", "ack_cleaning": "on", "ack_overtime": "on"}


def _wedding_url(res):
    """The wedding's own link: scoped to the order and the day, sent to the billing contact."""
    return reverse(
        "wedding_trip_confirm",
        args=[ack.make_wedding_day_ack_token(res.lead, res.pickup_date)],
    )


def test_a_wedding_day_shows_the_fee_terms_and_both_boxes(client):
    _fee("250")
    res = _wedding_res()

    body = client.get(_wedding_url(res)).content.decode()

    assert "Before you confirm" in body
    assert 'name="ack_cleaning"' in body and 'name="ack_overtime"' in body
    assert "$250.00" in body


def test_a_wedding_with_no_fee_shows_only_the_overtime_box(client):
    _fee("0")
    res = _wedding_res()

    body = client.get(_wedding_url(res)).content.decode()

    assert 'name="ack_overtime"' in body
    assert 'name="ack_cleaning"' not in body


def test_a_non_wedding_day_renders_unchanged(client):
    _fee("250")
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())

    body = client.get(_day_url(res)).content.decode()

    assert "Before you confirm" not in body
    assert 'name="ack_overtime"' not in body


def test_the_plain_day_link_never_asks_for_the_fees(client):
    """The booker's own link (or a planner holding it) must never be able to give the
    billing contact's acknowledgment."""
    _fee("250")
    res = _wedding_res()

    body = client.get(_day_url(res)).content.decode()
    client.post(_day_url(res), {"ack": "on", "ack_cleaning": "on", "ack_overtime": "on"})

    assert "Before you confirm" not in body
    res.lead.refresh_from_db()
    assert res.lead.fees_acknowledged_at is None


def test_a_wedding_confirm_is_refused_until_both_boxes_are_ticked(client):
    _fee("250")
    res = _wedding_res()

    resp = client.post(_wedding_url(res), {"ack": "on", "ack_overtime": "on"})

    assert resp.status_code == 200
    assert "acknowledge the cleaning fee and the overtime" in resp.content.decode()
    res.refresh_from_db()
    res.lead.refresh_from_db()
    assert res.customer_confirmed_at is None
    assert res.lead.fees_acknowledged_at is None


def test_an_empty_wedding_post_reports_both_missing_ticks_at_once(client):
    _fee("250")
    res = _wedding_res()

    body = client.post(_wedding_url(res), {}).content.decode()

    assert "confirm these details are accurate" in body
    assert "acknowledge the cleaning fee and the overtime" in body


def test_a_wedding_confirm_stamps_the_lead_with_ip_and_version(client):
    _fee("250")
    res = _wedding_res()

    # client_ip() reads the peer unless TRUSTED_PROXY_COUNT says a proxy is in front.
    resp = client.post(_wedding_url(res), ALL_BOXES, REMOTE_ADDR="203.0.113.9")

    assert resp.status_code == 302
    res.refresh_from_db()
    lead = res.lead
    lead.refresh_from_db()
    assert res.customer_confirmed_at is not None
    assert lead.fees_acknowledged_at is not None
    assert lead.fees_acknowledged_ip == "203.0.113.9"
    assert lead.fees_acknowledged_version == lead_services.fee_terms_version()


def test_reopening_shows_acknowledged_and_never_restamps(client):
    _fee("250")
    res = _wedding_res()
    url = _wedding_url(res)
    client.post(url, ALL_BOXES)
    res.lead.refresh_from_db()
    first = res.lead.fees_acknowledged_at

    body = client.get(url).content.decode()
    client.post(url, ALL_BOXES)
    res.lead.refresh_from_db()

    assert "Cleaning fee and overtime acknowledged on" in body
    assert 'name="ack_overtime"' not in body
    assert res.lead.fees_acknowledged_at == first


def test_a_day_confirmed_before_the_fees_can_still_acknowledge_them(client):
    """Weddings confirmed before APC-94 shipped have trips confirmed and fees pending."""
    _fee("250")
    res = _wedding_res()
    Reservation.objects.filter(pk=res.pk).update(customer_confirmed_at=timezone.now())
    url = _wedding_url(res)

    body = client.get(url).content.decode()
    assert 'name="ack_overtime"' in body and 'name="ack_cleaning"' in body
    assert 'name="ack"' not in body  # the trip details are already confirmed

    assert client.post(url, {"ack_cleaning": "on", "ack_overtime": "on"}).status_code == 302
    res.lead.refresh_from_db()
    assert res.lead.fees_acknowledged_at is not None


def test_a_mixed_day_keeps_the_wedding_and_the_plain_order_apart(client):
    _fee("250")
    contact = ContactFactory()
    wedding = _wedding_res(lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED))
    plain = ReservationFactory(
        lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED),
        pickup_date=wedding.pickup_date,
        stops=["Plain pickup", "Plain drop-off"],
    )

    wedding_body = client.get(_wedding_url(wedding)).content.decode()
    assert "Plain pickup" not in wedding_body  # only the wedding order's trips

    # The booker confirms their own trip with the one box; the wedding is not touched.
    assert client.post(_day_url(plain), {"ack": "on"}).status_code == 302
    plain.refresh_from_db()
    wedding.lead.refresh_from_db()
    assert plain.customer_confirmed_at is not None
    assert wedding.lead.fees_acknowledged_at is None


def test_the_page_names_the_billing_contact_on_a_wedding(client):
    _fee("250")
    booker = ContactFactory(name="Casey Booker")
    payer = ContactFactory(name="Pat Payer")
    res = _wedding_res(
        lead=LeadFactory(contact=booker, billing_contact=payer, status=Lead.Status.BOOKED)
    )

    body = client.get(_wedding_url(res)).content.decode()

    assert "Pat Payer" in body


def test_a_tampered_wedding_token_404s(client):
    assert client.get(reverse("wedding_trip_confirm", args=["nope"])).status_code == 404


# --- affiliate trip confirmation (APC-20) -------------------------------------------


def test_affiliate_confirm_stamps_the_assignment(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED)
    url = reverse("affiliate_trip_confirm", args=[ack.make_affiliate_ack_token(a)])

    assert client.get(url).status_code == 200
    client.post(url, {"ack": "on"})
    a.refresh_from_db()
    assert a.affiliate_confirmed_at is not None


def test_affiliate_confirm_inactive_assignment_does_not_stamp(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    a = AssignmentFactory(reservation=res, status=Assignment.Status.WITHDRAWN)
    url = reverse("affiliate_trip_confirm", args=[ack.make_affiliate_ack_token(a)])
    client.post(url, {"ack": "on"})
    a.refresh_from_db()
    assert a.affiliate_confirmed_at is None


# --- wedding day-of details (APC-18) ----------------------------------------------


def test_wedding_details_writes_the_lead_fields(client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    res = ReservationFactory(lead=lead, pickup_date=_future(), source_leg_id="guests-in")
    url = reverse("wedding_details", args=[ack.make_wedding_details_token(res)])

    assert client.get(url).status_code == 200
    resp = client.post(
        url,
        {
            "wedding_name": "Boyne–Ellis Wedding",
            "contact_name": "Jamie Planner",
            "contact_phone": "(703) 555-0148",
        },
    )
    assert resp.status_code == 302
    lead.refresh_from_db()
    assert lead.wedding_name == "Boyne–Ellis Wedding"
    assert lead.day_of_contact_name == "Jamie Planner"
    assert lead.day_of_contact_phone  # normalised to E.164 or kept as typed
