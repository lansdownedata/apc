"""`record_update` and the customer, affiliate and planner sources, end to end (APC-140 §5.1)."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.audit.explicit import record_update
from apps.audit.models import AuditEntry
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import holds
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.planners.models import PlannerRequest
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory, TransferReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _entries(model, pk):
    ct = ContentType.objects.get_for_model(model)
    return AuditEntry.objects.filter(content_type=ct, object_id=pk)


def _future(days=20):
    return (timezone.now() + timedelta(days=days)).date()


def _booked_lead(**kw):
    lead = LeadFactory(status=Lead.Status.QUOTED, contact=ContactFactory(), **kw)
    TransferReservationFactory(
        lead=lead,
        rate=Decimal("1000.00"),
        pickup_date=timezone.localdate() + timedelta(days=40),
        pickup_time=time(9, 0),
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.BOOKED)
    lead.refresh_from_db()
    return lead


# --- record_update ---------------------------------------------------------------------


def test_record_update_drops_excluded_and_unchanged():
    lead = LeadFactory()
    now = timezone.now()

    entry = record_update(
        lead,
        accepted_terms_at=now,
        accepted_terms_version="abcd1234",
        fees_acknowledged_ip="203.0.113.9",  # excluded from the trail
        updated_at=now,  # global exclude
        status=lead.status,  # unchanged
    )

    assert set(entry.changes) == {"accepted_terms_at", "accepted_terms_version"}
    assert entry.changes["accepted_terms_version"] == ["", "abcd1234"]
    assert entry.changes["accepted_terms_at"][0] == "None"
    assert entry.order_id == lead.pk


def test_record_update_noop_returns_none():
    lead = LeadFactory()
    before = AuditEntry.objects.count()

    assert record_update(lead, fees_acknowledged_ip="203.0.113.9", status=lead.status) is None
    assert AuditEntry.objects.count() == before


def test_record_update_ignores_an_unregistered_model():
    assert record_update(ServiceTypeFactory(), name="Other") is None


# --- customer stamps -------------------------------------------------------------------


def test_accept_terms_logs_once_with_quote_page_source(client):
    lead = _booked_lead()
    url = reverse("quote_terms", args=[lead_services.make_deposit_token(lead)])
    before = _entries(Lead, lead.pk).count()

    client.post(url, {"accept": "on"})
    client.post(url, {"accept": "on"})

    new = list(_entries(Lead, lead.pk).order_by("pk"))[before:]
    assert len(new) == 1
    assert new[0].source == "customer: quote page"
    assert new[0].action == AuditEntry.Action.UPDATE
    assert new[0].changes["accepted_terms_version"][1] == lead_services.terms_version()


def test_acknowledge_fees_ip_absent_from_changes(client):
    vehicle = VehicleTypeFactory()
    vehicle.cleaning_fee = Decimal("250")
    vehicle.save()
    lead = LeadFactory(status=Lead.Status.BOOKED)
    res = ReservationFactory(
        lead=lead,
        service_type=ServiceTypeFactory(name="Wedding Transportation"),
        pickup_date=_future(),
    )
    url = reverse(
        "wedding_trip_confirm", args=[ack.make_wedding_day_ack_token(lead, res.pickup_date)]
    )
    before = set(_entries(Lead, lead.pk).values_list("pk", flat=True))

    client.post(
        url,
        {"ack": "on", "ack_cleaning": "on", "ack_overtime": "on"},
        REMOTE_ADDR="203.0.113.9",
    )

    new = _entries(Lead, lead.pk).exclude(pk__in=before).get()
    assert new.source == "customer: trip confirmation"
    assert set(new.changes) == {"fees_acknowledged_at", "fees_acknowledged_version"}
    assert "203.0.113.9" not in str(new.changes)


def test_confirm_trip_day_logs_only_trips_this_call_stamped(client):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    day = _future()
    done = ReservationFactory(lead=lead, pickup_date=day)
    first = ReservationFactory(lead=lead, pickup_date=day)
    second = ReservationFactory(lead=lead, pickup_date=day)
    Reservation.objects.filter(pk=done.pk).update(customer_confirmed_at=timezone.now())
    url = reverse("trip_confirm", args=[ack.make_trip_day_ack_token(lead.contact, day)])
    before = AuditEntry.objects.count()

    client.post(url, {"ack": "on"})
    client.post(url, {"ack": "on"})

    assert AuditEntry.objects.count() == before + 2
    assert not _entries(Reservation, done.pk).filter(source="customer: trip confirmation")
    for trip in (first, second):
        entry = _entries(Reservation, trip.pk).get(source="customer: trip confirmation")
        assert entry.trip_id == trip.pk
        assert entry.order_id == lead.pk
        assert set(entry.changes) == {"customer_confirmed_at"}
        trip.refresh_from_db()
        assert entry.changes["customer_confirmed_at"][1] == str(
            trip.customer_confirmed_at.replace(tzinfo=None)
        )


def test_affiliate_confirm_source(client):
    res = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), pickup_date=_future())
    assignment = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED)
    url = reverse("affiliate_trip_confirm", args=[ack.make_affiliate_ack_token(assignment)])
    before = set(_entries(Assignment, assignment.pk).values_list("pk", flat=True))

    client.post(url, {"ack": "on"})

    entry = _entries(Assignment, assignment.pk).exclude(pk__in=before).get()
    assert entry.source == "affiliate: trip confirmation"
    assert "affiliate_confirmed_at" in entry.changes


def test_quote_page_view_writes_nothing(client):
    lead = _booked_lead()
    before = AuditEntry.objects.count()

    resp = client.get(reverse("quote_page", args=[lead_services.make_deposit_token(lead)]))

    assert resp.status_code == 200
    assert AuditEntry.objects.count() == before


# --- planner ---------------------------------------------------------------------------


def test_planner_request_entry_has_planner_email(client):
    from apps.accounts.factories import UserFactory
    from apps.leads.models import LeadContact
    from apps.planners import services as planner_services

    contact = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.notifications.email.deliver_html_email", return_value="pm-1"):
        account, _ = planner_services.invite(contact, actor=UserFactory())
    planner_services.register(account, "a-long-pass-phrase-77")
    client.post(
        reverse("planners:login"),
        {"email": "dana@example.com", "password": "a-long-pass-phrase-77"},
    )
    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Whitfield & Okafor")
    LeadContact.objects.create(lead=lead, contact=contact, role=LeadContact.Role.LEAD_PLANNER)
    ReservationFactory(lead=lead, pickup_date=_future())

    client.post(reverse("planners:request_change", args=[lead.pk]), {"about": "", "body": "Move."})

    req = PlannerRequest.objects.get()
    entry = _entries(PlannerRequest, req.pk).get()
    assert entry.action == AuditEntry.Action.CREATE
    assert entry.order_id == lead.pk
    assert entry.actor is None
    assert entry.actor_email == "dana@example.com"
    assert entry.source == "planner portal"


# --- cleaning hold ---------------------------------------------------------------------


def test_hold_transition_logs_once_and_loser_logs_nothing():
    charge = ChargeFactory(kind=Charge.Kind.HOLD, status=Charge.Status.PENDING)
    stale = Charge.objects.get(pk=charge.pk)
    before = _entries(Charge, charge.pk).count()

    assert holds._while_pending(charge, status=Charge.Status.AUTHORIZED) is True
    assert holds._while_pending(stale, status=Charge.Status.FAILED, failure_reason="x") is False

    new = list(_entries(Charge, charge.pk).order_by("pk"))[before:]
    assert len(new) == 1
    assert new[0].changes["status"] == [Charge.Status.PENDING, Charge.Status.AUTHORIZED]


# --- odd kwargs ------------------------------------------------------------------------


def test_record_update_fk_by_attname_logs_under_the_field_name():
    charge = ChargeFactory()
    trip = ReservationFactory()

    entry = record_update(charge, reservation_id=trip.pk)

    assert list(entry.changes) == ["reservation"]
    assert entry.changes["reservation"][1] == str(trip.pk)


def test_record_update_excluded_fk_by_attname_logs_nothing_and_does_not_raise():
    charge = ChargeFactory()

    assert record_update(charge, receipt_parent_id=5) is None


def test_record_update_unknown_name_is_ignored():
    charge = ChargeFactory()

    assert record_update(charge, no_such_column=1) is None


def test_record_update_writes_nothing_when_auditlog_is_disabled():
    from auditlog.context import disable_auditlog

    lead = LeadFactory()
    before = AuditEntry.objects.count()

    with disable_auditlog():
        assert record_update(lead, accepted_terms_version="abcd1234") is None

    assert AuditEntry.objects.count() == before


def test_hold_transition_excluded_columns_stay_out_of_changes():
    charge = ChargeFactory(kind=Charge.Kind.HOLD, status=Charge.Status.PENDING)
    now = timezone.now()

    holds._while_pending(
        charge, status=Charge.Status.AUTHORIZED, authorized_at=now, capture_expires_at=now
    )

    entry = _entries(Charge, charge.pk).order_by("pk").last()
    assert set(entry.changes) == {"status"}
