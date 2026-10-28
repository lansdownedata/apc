"""APC-105: the customer's side of an order billed to an account.

No deposit and no pay step: the quote page's Book accepts the terms and books. Billing to
an account is chosen by staff only — the customer is never offered it.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services as payment_services
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _quote(*, account=True, status=Lead.Status.QUOTED):
    lead = LeadFactory(
        status=status,
        contact=ContactFactory(),
        quote_expires_at=timezone.now() + timezone.timedelta(days=10),
    )
    TransferReservationFactory(lead=lead, rate=Decimal("1000.00"))
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    if account:
        lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
        lead.save(update_fields=["billing_group"])
    return lead


def _url(name, lead):
    return reverse(name, args=[services.make_deposit_token(lead)])


def test_the_quote_page_offers_book_with_the_terms_and_no_deposit(client):
    html = client.get(_url("quote_page", _quote())).content.decode()

    assert _url("quote_book_on_account", Lead.objects.get()) in html
    assert 'name="accept"' in html
    assert "row deposit" not in html
    assert "Book Now" not in html


def test_a_card_quote_still_goes_to_the_pay_page(client):
    lead = _quote(account=False)
    html = client.get(_url("quote_page", lead)).content.decode()

    assert _url("quote_pay", lead) in html
    assert "row deposit" in html


def test_book_without_the_terms_box_is_refused(client):
    lead = _quote()
    resp = client.post(_url("quote_book_on_account", lead))

    assert resp.status_code == 200
    assert "agree to the terms" in resp.content.decode()
    assert Lead.objects.get(pk=lead.pk).status == Lead.Status.QUOTED


def test_book_with_the_terms_box_accepts_them_and_books(client):
    lead = _quote()
    resp = client.post(_url("quote_book_on_account", lead), {"accept": "on"})

    assert resp.status_code == 302
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED
    assert lead.accepted_terms_at is not None
    assert not lead.payment.charges.exists()


def test_a_card_quote_cannot_book_without_paying(client):
    lead = _quote(account=False)
    assert client.post(_url("quote_book_on_account", lead), {"accept": "on"}).status_code == 404
    assert Lead.objects.get(pk=lead.pk).status == Lead.Status.QUOTED


def test_the_pay_page_has_nothing_to_pay(client):
    html = client.get(_url("quote_pay", _quote(status=Lead.Status.BOOKED))).content.decode()
    assert "Billed to your account. Nothing to pay here." in html


def test_no_intent_opens_for_an_account_order(client):
    lead = _quote()
    resp = client.post(_url("quote_pay_intent", lead), {"accept_terms": "1"})
    assert resp.status_code == 400
    assert not lead.payment.charges.exists()


@pytest.mark.parametrize(
    "call",
    [
        lambda lead: payment_services.open_intent_for(lead.payment, kind="balance", amount=10),
        lambda lead: payment_services.create_admin_payment_intent(lead.payment, "10.00"),
        lambda lead: payment_services.charge_saved_card(lead.payment, "10.00"),
        lambda lead: payment_services.charge_balance(lead.payment),
        lambda lead: payment_services.send_pay_link(lead, base_url="https://x", sender_name="A"),
    ],
)
def test_the_card_services_refuse_an_account_order(call, settings):
    settings.OUTBOUND_ENABLED = True
    lead = _quote(status=Lead.Status.BOOKED)
    lead.payment.stripe_payment_method_id = "pm_1"
    lead.payment.stripe_customer_id = "cus_1"
    lead.payment.save()

    with (
        patch("apps.payments.services._stripe") as stripe,
        pytest.raises(payment_services.PaymentError, match="billed to an account"),
    ):
        call(lead)
    stripe.assert_not_called()


def test_a_booked_account_order_reads_as_booked_on_the_quote_page(client):
    html = client.get(_url("quote_page", _quote(status=Lead.Status.BOOKED))).content.decode()

    assert "already booked" in html
    assert "Book Now" not in html
    assert 'name="accept"' not in html


def test_book_404s_for_an_expired_or_booked_account_quote(client):
    expired = _quote()
    Lead.objects.filter(pk=expired.pk).update(
        quote_expires_at=timezone.now() - timezone.timedelta(days=1)
    )
    booked = _quote(status=Lead.Status.BOOKED)
    for lead in (expired, booked):
        assert client.post(_url("quote_book_on_account", lead), {"accept": "on"}).status_code == 404


def test_terms_already_accepted_are_not_asked_again(client):
    lead = _quote()
    services.accept_terms(lead)

    html = client.get(_url("quote_page", lead)).content.decode()
    assert 'name="accept"' not in html
    assert client.post(_url("quote_book_on_account", lead)).status_code == 302
    assert Lead.objects.get(pk=lead.pk).status == Lead.Status.BOOKED
