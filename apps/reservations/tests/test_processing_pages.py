"""APC-102 — the Trip Processing screen and its endpoints."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview

pytestmark = pytest.mark.django_db
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def accountant(client):
    user = UserFactory(can_manage_payments=True)
    client.force_login(user)
    client.user = user
    return client


def _trip(status=S.DONE, *, contact=None, lead=None, days_ago=1, **kw):
    lead = lead or LeadFactory(status=Lead.Status.BOOKED, contact=contact or ContactFactory())
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_time=time(10, 0),
        pickup_timezone="America/Los_Angeles",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _paid(trip):
    plan = PaymentPlanFactory(lead=trip.lead, quote_total=trip.line_total)
    plan.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=trip.line_total,
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_full",
        idempotency_key=f"bal-{trip.pk}",
    )
    ledger.post_capture(
        lead=trip.lead,
        amount=trip.line_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"paid-{trip.pk}",
    )
    return plan


def _page(client, **query):
    return client.get(reverse("processing"), query)


def test_the_screen_needs_a_login(client):
    assert _page(client).status_code == 302


def test_the_list_groups_trips_by_customer(staff):
    trip = _trip(contact=ContactFactory(name="Priya Okafor"))
    _trip(lead=trip.lead, days_ago=2)

    body = _page(staff).content.decode()

    assert "Priya Okafor" in body and trip.reference in body
    assert "Ready to process" in body and "<dialog" not in body


def test_selecting_a_trip_renders_its_pane_with_price_lines_and_edit_price(staff):
    trip = _trip(gratuity_pct=Decimal("20"))
    _paid(trip)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "data-price-lines" in body and "Gratuity (20%)" in body
    assert "Edit price" in body and "reservation-edit" in body
    assert "trip-review-close" not in body  # the pop-up's close button is gone here
    assert "PDT" in body or "PST" in body  # the trip's own zone


def test_a_deep_link_opens_its_trip_whatever_the_filters(staff):
    trip = _trip(days_ago=200)
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    body = _page(staff, trip=trip.pk, view="ready").content.decode()

    assert "data-price-lines" in body and "Processed" in body


def test_the_footer_offers_the_right_way_forward(accountant):
    trip = _trip()
    _paid(trip)
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "processingPane(" in body and "Process and send pay link" in body
    assert "To charge now" in body and "200.00" in body


def test_a_trip_without_a_status_offers_the_status_buttons(accountant):
    trip = _trip(S.CUSTOMER_IN_CAR)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert "No final status arrived" in body and "No Show" in body


def test_without_payments_access_the_footer_only_reads(staff):
    trip = _trip()
    _paid(trip)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "Processing needs payments access" in body and "processNone()" not in body


def test_the_processed_pane_shows_the_banner_history_and_unlock(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "Times and charges are locked" in body and "History" in body
    assert "Unlock to edit" in body and "Add a charge" in body


@pytest.mark.parametrize(
    "name",
    ["processing_process", "processing_unlock", "processing_add_charge", "processing_refund"],
)
def test_the_actions_need_payments_access(staff, name):
    trip = _trip()
    assert staff.post(reverse(name, args=[trip.pk])).status_code == 403


def test_process_endpoint_locks_and_reports_errors_as_json(accountant):
    trip = _trip()
    _paid(trip)

    bad = accountant.post(reverse("processing_process", args=[trip.pk]), {"expected": "9.99"})
    ok = accountant.post(reverse("processing_process", args=[trip.pk]), {"expected": "0.00"})

    assert bad.status_code == 400 and "price changed" in bad.json()["error"]
    assert ok.json()["ok"] is True
    assert TripReview.objects.get(reservation=trip).is_processed


def test_unlock_endpoint_needs_a_reason(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    resp = accountant.post(reverse("processing_unlock", args=[trip.pk]), {"reason": ""})

    assert resp.status_code == 400 and "why" in resp.json()["error"]


def test_set_status_marks_what_happened(staff):
    trip = _trip(S.CUSTOMER_IN_CAR)

    resp = staff.post(reverse("processing_set_status", args=[trip.pk]), {"status": "no_show"})

    trip.refresh_from_db()
    assert resp.json()["ok"] is True and trip.trip_status == S.NO_SHOW


def test_set_status_refuses_anything_but_the_three(staff):
    trip = _trip(S.CUSTOMER_IN_CAR)
    resp = staff.post(reverse("processing_set_status", args=[trip.pk]), {"status": "cancelled"})
    assert resp.status_code == 400


def test_the_nav_shows_processing(staff):
    body = staff.get(reverse("dashboard")).content.decode()
    assert reverse("processing") in body and ">Processing<" in body


def test_the_list_costs_a_flat_number_of_queries(staff):
    _trip()
    _page(staff)
    with CaptureQueriesContext(connection) as one:
        _page(staff)
    for _ in range(30):
        _trip(contact=ContactFactory())
    with CaptureQueriesContext(connection) as many:
        _page(staff)

    assert len(many) == len(one)


# --- branch-review fixes -------------------------------------------------------------------


def test_a_processed_trip_whose_balance_moved_says_so(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")
    Reservation.objects.filter(pk=trip.pk).update(overtime_hours=Decimal("1"))  # LA edit, etc.

    body = _page(accountant, trip=trip.pk, view="processed").content.decode()

    assert "has moved since it was processed" in body and "200.00 now owed" in body


def test_the_issues_drill_down_lists_every_trip_with_an_open_issue(staff):
    from apps.reservations.models import TripIssue

    open_one = _trip(contact=ContactFactory(name="Flagged Customer"))
    _trip(contact=ContactFactory(name="Quiet Customer"))
    review = TripReview.objects.create(reservation=open_one, processed_at=timezone.now())
    TripIssue.objects.create(review=review, category="driver", severity="low", note="Late")

    body = _page(staff, issues="1").content.decode()

    assert "Flagged Customer" in body and "Quiet Customer" not in body
    from apps.tasks import exceptions

    assert exceptions.counts()["open_issues"] == 1


def test_a_deep_link_to_a_trip_with_nothing_to_process_says_why(staff):
    trip = _trip(S.CANCELLED)

    body = _page(staff, trip=trip.pk).content.decode()

    assert "has nothing to process" in body


def test_the_old_order_url_opens_its_first_workable_trip(staff):
    cancelled = _trip(S.CANCELLED, days_ago=3)
    done = _trip(lead=cancelled.lead, days_ago=2)

    resp = staff.get(reverse("trip_review_order", args=[cancelled.lead_id]))

    assert resp.url == f"{reverse('processing')}?trip={done.pk}"


def test_processed_refund_is_refused_on_an_order_that_is_not_booked(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")
    Lead.objects.filter(pk=trip.lead_id).update(status=Lead.Status.LOST)

    resp = accountant.post(
        reverse("processing_refund", args=[trip.pk]), {"amount": "50", "reason": "x"}
    )

    assert resp.status_code == 400


def test_a_bad_refund_amount_is_a_message_not_a_500(accountant):
    trip = _trip()
    _paid(trip)
    processing.process(trip, user=accountant.user, expected="0.00")

    resp = accountant.post(
        reverse("processing_refund", args=[trip.pk]), {"amount": "NaN", "reason": "x"}
    )

    assert resp.status_code == 400 and "valid amount" in resp.json()["error"]


def test_save_errors_have_a_place_to_show_in_processing(accountant):
    trip = _trip()
    _paid(trip)

    body = _page(accountant, trip=trip.pk).content.decode()

    assert 'x-text="error"' in body.split("processingPane(")[1]


def test_the_start_date_covers_post_trip_work_already_open():
    import importlib

    from django.apps import apps as django_apps

    from apps.tasks.models import Task, TaskConfig

    module = importlib.import_module("apps.tasks.migrations.0012_processing_from")
    old = _trip(days_ago=20)
    Task.objects.create(lead=old.lead, reservation=old, kind="ops_review", opens_at=timezone.now())
    TaskConfig.load()
    TaskConfig.objects.update(processing_from=None)

    module.start_today(django_apps, None)

    assert TaskConfig.load().processing_from == old.pickup_date
