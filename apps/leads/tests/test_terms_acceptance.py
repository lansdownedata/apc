"""APC-55 — terms acceptance at checkout is the contract, so it's recorded.

The deposit can't be opened without it (server-side), the first acceptance is stamped
with the terms version and never overwritten, and it closes the `contract_signed` task.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import TransferReservationFactory
from apps.tasks import services as tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

OPEN_INTENT = "apps.leads.views.payment_services.open_intent_for"


def _lead(**kwargs):
    kwargs.setdefault("status", Lead.Status.QUOTED)
    kwargs.setdefault("contact", ContactFactory())
    kwargs.setdefault("quote_expires_at", timezone.now() + timedelta(days=10))
    lead = LeadFactory(**kwargs)
    TransferReservationFactory(
        lead=lead,
        rate=Decimal("1000.00"),
        pickup_date=timezone.localdate() + timedelta(days=40),
        pickup_time=time(9, 0),
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    return lead


def _intent_url(lead):
    return reverse("quote_pay_intent", args=[services.make_deposit_token(lead)])


def test_the_deposit_intent_is_refused_without_acceptance(client):
    lead = _lead()

    with patch(OPEN_INTENT) as opened:
        resp = client.post(_intent_url(lead))

    assert resp.status_code == 400
    assert "terms" in resp.json()["error"].lower()
    opened.assert_not_called()
    lead.refresh_from_db()
    assert lead.accepted_terms_at is None


def test_acceptance_stamps_time_and_version_once(client):
    lead = _lead()
    with patch(OPEN_INTENT, return_value=(MagicMock(pk=1), "secret")):
        assert client.post(_intent_url(lead), {"accept_terms": "1"}).status_code == 200
    lead.refresh_from_db()
    first_at = lead.accepted_terms_at
    assert first_at is not None
    assert lead.accepted_terms_version == services.terms_version()
    assert len(lead.accepted_terms_version) == 8

    Lead.objects.filter(pk=lead.pk).update(accepted_terms_version="oldver01")
    with patch(OPEN_INTENT, return_value=(MagicMock(pk=1), "secret")):
        assert client.post(_intent_url(lead), {"accept_terms": "1"}).status_code == 200

    lead.refresh_from_db()
    assert lead.accepted_terms_at == first_at
    assert lead.accepted_terms_version == "oldver01"


def test_a_customer_who_already_accepted_is_not_asked_again(client):
    lead = _lead(accepted_terms_at=timezone.now(), accepted_terms_version="abcd1234")

    with patch(OPEN_INTENT, return_value=(MagicMock(pk=1), "secret")):
        resp = client.post(_intent_url(lead))

    assert resp.status_code == 200


def test_the_terms_version_is_a_stable_hash_of_the_rendered_terms():
    assert services.terms_version() == services.terms_version()
    assert services.terms_version().isalnum()


def test_the_pay_page_carries_the_checkbox_and_the_terms(client):
    lead = _lead()

    body = client.get(reverse("quote_pay", args=[services.make_deposit_token(lead)])).content
    html = body.decode()

    assert 'id="accept-terms"' in html
    assert "I agree to the" in html
    assert "Motor Coach Equipment" in html  # the same terms the quote page shows


def test_the_pay_page_skips_the_checkbox_once_accepted(client):
    lead = _lead(accepted_terms_at=timezone.now(), accepted_terms_version="abcd1234")

    html = client.get(reverse("quote_pay", args=[services.make_deposit_token(lead)])).content

    assert b'id="accept-terms"' not in html


def test_the_quote_page_still_shows_the_same_terms(client):
    lead = _lead()

    html = client.get(reverse("quote_page", args=[services.make_deposit_token(lead)])).content

    assert b"Motor Coach Equipment" in html


# --- the contract_signed task ------------------------------------------------------


def _booked(**kwargs):
    lead = _lead(**kwargs)
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.BOOKED)
    lead.refresh_from_db()
    tasks.ensure_tasks(lead)
    return lead


def test_acceptance_closes_contract_signed():
    lead = _booked(accepted_terms_at=timezone.now(), accepted_terms_version="abcd1234")

    task = Task.objects.get(lead=lead, kind="contract_signed")
    assert task.status == Task.Status.DONE
    assert task.completed_by is None


def test_a_booked_order_without_a_stamp_keeps_contract_signed_open():
    lead = _booked()

    assert Task.objects.get(lead=lead, kind="contract_signed").status == Task.Status.OPEN


def test_staff_can_check_contract_signed_off_by_hand():
    lead = _booked()
    task = Task.objects.get(lead=lead, kind="contract_signed")

    tasks.complete(task, user=UserFactory(), note="Signed terms by email")
    tasks.evaluate_lead(lead)

    task.refresh_from_db()
    assert task.status == Task.Status.DONE


def test_the_workspace_shows_the_acceptance(client):
    client.force_login(UserFactory())
    lead = _booked(accepted_terms_at=timezone.now(), accepted_terms_version="abcd1234")

    html = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert "Terms accepted" in html
    assert "vabcd1234" in html


# --- the terms link for staff and phone bookings (APC-93) -----------------------------


def _terms_url(lead):
    return reverse("quote_terms", args=[services.make_deposit_token(lead)])


def test_the_terms_link_shows_the_order_and_the_terms(client):
    lead = _booked()

    html = client.get(_terms_url(lead)).content.decode()

    assert lead.quote_no in html
    assert "I agree to the terms" in html
    assert "Cancellation Policy" in html  # the same T&Cs the pay page shows
    assert lead.reservations.get().vehicle.name in html  # the trip sheet renders


def test_accepting_stamps_the_lead_and_closes_contract_signed(client):
    lead = _booked()

    resp = client.post(_terms_url(lead), {"accept": "on"})

    assert resp.status_code == 302
    lead.refresh_from_db()
    assert lead.accepted_terms_at is not None
    assert lead.accepted_terms_version == services.terms_version()
    assert Task.objects.get(lead=lead, kind="contract_signed").status == Task.Status.DONE


def test_an_accepted_order_shows_accepted_on_instead_of_the_form(client):
    lead = _booked(accepted_terms_at=timezone.now(), accepted_terms_version="abcd1234")

    html = client.get(_terms_url(lead)).content.decode()

    assert "You accepted the terms on" in html
    assert 'name="accept"' not in html


def test_a_tampered_terms_token_404s(client):
    assert client.get(reverse("quote_terms", args=["nope"])).status_code == 404


def test_a_bare_terms_post_does_not_stamp(client):
    lead = _booked()

    resp = client.post(_terms_url(lead), {})

    assert resp.status_code == 200
    assert "Please tick the box" in resp.content.decode()
    lead.refresh_from_db()
    assert lead.accepted_terms_at is None


def test_a_lost_order_has_nothing_to_accept(client):
    lead = _booked()
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.LOST)

    html = client.get(_terms_url(lead)).content.decode()
    client.post(_terms_url(lead), {"accept": "on"})

    assert "nothing to accept" in html.lower()
    assert 'name="accept"' not in html
    lead.refresh_from_db()
    assert lead.accepted_terms_at is None


def test_make_terms_page_url_round_trips():
    lead = _booked()

    url = services.make_terms_page_url(lead, base_url="https://apc.example.com")

    token = url.rstrip("/").split("/")[-2]
    assert url.startswith("https://apc.example.com/quote/") and url.endswith("/terms/")
    assert services.read_deposit_token(token) == lead


def test_a_quoted_order_cannot_accept_by_link(client):
    """The pay page is where a quote accepts; pre-accepting here would hide its checkbox."""
    lead = _lead()  # QUOTED

    html = client.get(_terms_url(lead)).content.decode()
    client.post(_terms_url(lead), {"accept": "on"})

    assert 'name="accept"' not in html
    assert "nothing to accept" in html.lower()
    lead.refresh_from_db()
    assert lead.accepted_terms_at is None
