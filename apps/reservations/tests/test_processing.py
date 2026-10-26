"""APC-102 — Trip Processing: each finished trip worked on its own (spec 2026-10-25 §4).

Stripe and Podium are mocked throughout; nothing leaves the test.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
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


def _trip(status=S.DONE, *, days_ago=1, contact=None, lead=None, **kw):
    lead = lead or LeadFactory(status=Lead.Status.BOOKED, contact=contact or ContactFactory())
    kw.setdefault("trip_type", "hourly")
    kw.setdefault("rate", Decimal("200"))
    kw.setdefault("hours", Decimal("3"))
    kw.setdefault("min_hours", 0)
    kw.setdefault("pickup_time", time(10, 0))
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_timezone="America/New_York",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    return trip


def _ready(**query):
    return processing.ready_trips(processing.Filters.from_query(query))


# --- Task 1: what is ready ---------------------------------------------------------------


@pytest.mark.parametrize("status", [S.DONE, S.LATE_CANCEL, S.NO_SHOW])
def test_done_late_cancel_and_no_show_are_ready(status):
    trip = _trip(status)
    assert trip in _ready()


@pytest.mark.parametrize("status", [S.CANCELLED, S.COVID_CANCELLATION])
def test_plain_cancelled_and_covid_never_are(status):
    assert _trip(status) not in _ready()


def test_cancelled_by_affiliate_is_ready_only_with_money_to_settle():
    owes = _trip(S.CANCELLED_BY_AFFILIATE)
    free = _trip(S.CANCELLED_BY_AFFILIATE, rate=Decimal("0"))

    ready = _ready()

    assert owes in ready and free not in ready


def test_an_ended_trip_with_no_status_is_ready_and_needs_a_status():
    trip = _trip("", days_ago=1)

    (found,) = [t for t in _ready() if t.pk == trip.pk]

    assert found.needs_status is True


def test_a_trip_inside_the_grace_with_no_status_is_not_ready():
    trip = _trip("", days_ago=0, pickup_time=None)
    trip.pickup_time = (timezone.localtime() - timedelta(minutes=10)).time()
    trip.save(update_fields=["pickup_time"])

    assert trip not in _ready()


def test_a_processed_trip_leaves_ready_and_shows_in_processed():
    trip = _trip()
    TripReview.objects.create(reservation=trip, processed_at=timezone.now())

    assert trip not in _ready()
    assert trip in processing.processed_trips(processing.Filters.from_query({"view": "processed"}))


def test_only_booked_orders_count():
    quoted = _trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    booked = _trip(days_ago=2)

    ready = _ready()
    assert booked in ready and quoted not in ready


def test_ready_has_no_rolling_window_but_starts_at_the_processing_date():
    """A trip never drops off Ready unprocessed; trips before `processing_from` (settled the
    old way before the deploy) never show."""
    from apps.tasks.models import TaskConfig

    old = _trip(days_ago=60)
    before = _trip(days_ago=400)
    config = TaskConfig.load()
    config.processing_from = timezone.localdate() - timedelta(days=90)
    config.save()

    ready = _ready()
    assert old in ready and before not in ready


def test_the_date_filter_narrows_ready_and_processed_defaults_to_30_days():
    old = _trip(days_ago=60)
    recent = _trip(days_ago=2)
    assert _ready(**{"from": (timezone.localdate() - timedelta(days=7)).isoformat()}) == [recent]
    for t in (old, recent):
        TripReview.objects.create(reservation=t, processed_at=timezone.now())

    shown = processing.processed_trips(processing.Filters.from_query({"view": "processed"}))
    assert shown == [recent]


def test_a_trip_left_at_a_live_status_after_it_ended_needs_a_status():
    """The parallel-run gap: LA never sent Done, the trip sits at Customer in car."""
    trip = _trip(S.CUSTOMER_IN_CAR, days_ago=1)

    (found,) = [t for t in _ready() if t.pk == trip.pk]

    assert found.needs_status is True


def test_the_issues_filter_shows_trips_with_an_open_issue():
    from apps.reservations.models import TripIssue

    flagged = _trip()
    _trip()
    review = TripReview.objects.create(reservation=flagged)
    TripIssue.objects.create(review=review, category="driver", severity="low", note="Late")

    assert _ready(issues="1") == [flagged]


@pytest.mark.parametrize(
    "status,days_ago,expected",
    [
        (S.DONE, 1, True),
        (S.NO_SHOW, 1, True),
        (S.LATE_CANCEL, 1, True),
        (S.CANCELLED_BY_AFFILIATE, 1, True),
        (S.CANCELLED, 1, False),
        ("", 1, True),
        ("", -2, False),
    ],
)
def test_workable(status, days_ago, expected):
    assert processing.workable(_trip(status, days_ago=days_ago)) is expected


def test_a_trip_on_an_order_that_is_not_booked_is_not_workable():
    assert processing.workable(_trip(lead=LeadFactory(status=Lead.Status.LOST))) is False


def test_search_by_customer_name_or_trip_reference():
    mine = _trip(contact=ContactFactory(name="Priya Okafor"))
    other = _trip(contact=ContactFactory(name="Marcus Bell"))

    assert _ready(q="okafor") == [mine]
    assert _ready(q=other.reference.lower()) == [other]


def test_groups_are_customers_sorted_by_their_oldest_trip():
    a = ContactFactory(name="A")
    b = ContactFactory(name="B")
    a2 = _trip(contact=a, days_ago=1)
    _trip(contact=b, days_ago=5)
    a1 = _trip(lead=a2.lead, days_ago=3)
    PaymentPlanFactory(lead=a2.lead, card_last4="4242", stripe_payment_method_id="pm_1")

    groups = processing.group_by_customer(_ready())

    assert [g.contact for g in groups] == [b, a]
    assert groups[1].trips == [a1, a2]
    assert groups[1].pays == "Card ••4242" and groups[0].pays == "No card"


def test_ready_trips_cost_a_flat_number_of_queries():
    _trip()
    _trip(S.CANCELLED_BY_AFFILIATE, contact=ContactFactory())

    def touch():
        for g in processing.group_by_customer(_ready()):
            _ = g.pays
            for t in g.trips:
                t.line_total, t.reference, list(t.stops.all()), t.active_list

    touch()  # the first run creates the TaskConfig singleton; measure after
    with CaptureQueriesContext(connection) as one:
        touch()
    for _ in range(50):
        _trip(contact=ContactFactory())
    with CaptureQueriesContext(connection) as many:
        touch()

    assert len(many) == len(one), [q["sql"][:120] for q in many.captured_queries]
