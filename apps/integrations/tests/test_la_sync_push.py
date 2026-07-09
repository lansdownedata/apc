"""push_reservation / push_lead_bookings — preview, live, idempotency, failure alerts."""

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.contacts.factories import ContactFactory
from apps.integrations import la_sync
from apps.integrations.models import LACustomer, ZapEvent
from apps.leads.factories import LeadFactory
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory, StopFactory

pytestmark = pytest.mark.django_db


def _lead(email="jane@example.com"):
    return LeadFactory(contact=ContactFactory(name="Jane Doe", email=email))


def _reservation(lead):
    # stops=[] suppresses the factory's default (un-geocoded) stops — see
    # test_la_sync_payloads.py; pickup_date/pickup_time are required by _pickup_at.
    res = ReservationFactory(
        lead=lead, stops=[], pickup_date=date(2026, 7, 15), pickup_time=time(10, 0)
    )
    StopFactory(
        reservation=res, sequence=0, address="A", latitude=Decimal("1.0"), longitude=Decimal("2.0")
    )
    StopFactory(
        reservation=res, sequence=1, address="B", latitude=Decimal("3.0"), longitude=Decimal("4.0")
    )
    return res


# --- preview mode (LA not configured — the default test settings) ---


def test_preview_records_payloads_without_sending():
    res = _reservation(_lead())
    with patch.object(la_sync.limoanywhere, "requests", create=True) as req:
        event = la_sync.push_reservation(res)
    req.assert_not_called()
    assert event.result == ZapEvent.Result.PREVIEW
    assert "registration" in event.payload
    assert "rate_lookup" in event.payload
    assert "booking" in event.payload
    assert event.payload["registration"]["password"] == "(generated at send time)"


def test_preview_reruns_refresh_the_same_event():
    res = _reservation(_lead())
    first = la_sync.push_reservation(res)
    second = la_sync.push_reservation(res)
    assert first.pk == second.pk
    assert ZapEvent.objects.count() == 1


# --- live mode ---


@pytest.fixture
def live(settings):
    settings.LA_CLIENT_ID = "cid"
    settings.LA_CLIENT_SECRET = "cs"
    settings.LA_COMPANY_ALIAS = "allpro"
    settings.LA_PAYMENT_TYPE_ID = 7
    la_sync.limoanywhere._token_cache.clear()


def test_live_push_books_and_stores_ids(live):
    res = _reservation(_lead())
    with (
        patch.object(la_sync.limoanywhere, "register_customer") as register,
        patch.object(la_sync.limoanywhere, "get_token", return_value="tok"),
        patch.object(la_sync.limoanywhere, "subscribe_webhook"),
        patch.object(la_sync.limoanywhere, "rate_lookup") as rate,
        patch.object(la_sync.limoanywhere, "create_booking") as book,
    ):
        register.return_value = {"id": 12345, "number": "99119924"}
        rate.return_value = {"results": [{"id": 555, "total_amount": 0}]}
        book.return_value = {"id": 67890, "confirmation_number": "ABC123"}
        event = la_sync.push_reservation(res)

    assert event.result == ZapEvent.Result.SUCCESS
    res.refresh_from_db()
    assert res.la_reservation_id == "67890"
    assert res.la_confirmation == "ABC123"
    lac = LACustomer.objects.get(contact=res.lead.contact)
    assert lac.la_customer_id == "12345"
    res.lead.contact.refresh_from_db()
    assert res.lead.contact.la_account_id == "99119924"


def test_success_is_never_resent(live):
    res = _reservation(_lead())
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"{la_sync.IDEMPOTENCY_PREFIX}{res.pk}",
        result=ZapEvent.Result.SUCCESS,
    )
    with patch.object(la_sync.limoanywhere, "create_booking") as book:
        event = la_sync.push_reservation(res)
    book.assert_not_called()
    assert event.result == ZapEvent.Result.SUCCESS


def test_no_email_fails_with_alert(live):
    res = _reservation(_lead(email=""))
    event = la_sync.push_reservation(res)
    assert event.result == ZapEvent.Result.ERROR
    assert Notification.objects.filter(kind=Notification.Kind.SYNC_FAILED).exists()


def test_empty_rate_results_fail_with_clear_message(live):
    res = _reservation(_lead())
    with (
        patch.object(
            la_sync.limoanywhere, "register_customer", return_value={"id": 1, "number": "2"}
        ),
        patch.object(la_sync.limoanywhere, "get_token", return_value="tok"),
        patch.object(la_sync.limoanywhere, "subscribe_webhook"),
        patch.object(la_sync.limoanywhere, "rate_lookup", return_value={"results": []}),
    ):
        event = la_sync.push_reservation(res)
    assert event.result == ZapEvent.Result.ERROR
    assert "rate" in event.response.lower()


def test_error_then_retry_succeeds(live):
    res = _reservation(_lead())
    with (
        patch.object(
            la_sync.limoanywhere, "register_customer", return_value={"id": 1, "number": "2"}
        ),
        patch.object(la_sync.limoanywhere, "get_token", return_value="tok"),
        patch.object(la_sync.limoanywhere, "subscribe_webhook"),
        patch.object(
            la_sync.limoanywhere,
            "rate_lookup",
            side_effect=la_sync.limoanywhere.LAAPIError(500, "boom"),
        ),
    ):
        assert la_sync.push_reservation(res).result == ZapEvent.Result.ERROR

    with (
        patch.object(la_sync.limoanywhere, "get_token", return_value="tok"),
        patch.object(la_sync.limoanywhere, "rate_lookup", return_value={"results": [{"id": 9}]}),
        patch.object(
            la_sync.limoanywhere,
            "create_booking",
            return_value={"id": 42, "confirmation_number": "Z9"},
        ),
    ):
        assert la_sync.retry_failed_pushes() == 1
    assert ZapEvent.objects.get().result == ZapEvent.Result.SUCCESS


def test_push_lead_bookings_pushes_every_reservation():
    lead = _lead()
    _reservation(lead)
    _reservation(lead)
    events = la_sync.push_lead_bookings(lead)
    assert len(events) == 2
    assert ZapEvent.objects.count() == 2
