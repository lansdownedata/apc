"""Golden recording of the staff-click sends: what each button sends today, word for word
(APC-120).

Recorded once on the senders as they stood before they moved onto message rows (Send quote,
Send payment link, Send terms, the trip receipt, the affiliate offer and the affiliate
cancellation). `golden/staff_click.json` is never re-recorded after that: a failure here
means the move changed what a customer or an affiliate receives, or who receives it.

Each scenario builds a fixed order on the frozen clock of `golden/scenarios.py` and clicks
the button through its service. Every send is captured at the transport: Podium texts and
emails, and the text part of every email handed to Django's mail backend. Texts are
compared whitespace-normalised (the email layouts differ in line breaks, never in words).

Record (once only): GOLDEN_RECORD=1 pytest apps/messaging/tests/test_staff_click_golden.py
"""

import json
import os
import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads import services as lead_services
from apps.payments import receipts
from apps.payments import services as payments
from apps.payments.models import Charge, PaymentPlan
from apps.vendors.factories import VendorFactory

from .golden import scenarios as s

pytestmark = pytest.mark.django_db

EXPECTED = Path(__file__).parent / "golden" / "staff_click.json"
RECORD = os.environ.get("GOLDEN_RECORD") == "1"
SEND = "apps.integrations.podium.send_message"
AGENT = "Jordan Ellis"
_RECEIPT_NO = re.compile(r"\bR-\d+\b")


@pytest.fixture(autouse=True)
def _live(settings):
    settings.TOUCHPOINTS_ENABLED = False  # only the click itself sends
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = s.BASE_URL
    settings.TIME_ZONE = "America/New_York"
    settings.COMPANY_NAME = "All Pro Charter"
    settings.COMPANY_PHONE = "(703) 555-0100"
    settings.COMPANY_EMAIL = "reservations@allprocharter.com"
    settings.BALANCE_CHARGE_DAYS_BEFORE = 30
    settings.QUOTE_EXPIRY_DAYS_BEFORE_PICKUP = 14


# --- orders ----------------------------------------------------------------------------


def _quotable(person=s.DANA, **order):
    lead = s._order(person, **order)
    s._trip(lead, s.PLAIN_DAY, time(14, 0))
    s._trip(lead, s.PLAIN_DAY, time(19, 30), route=tuple(reversed(s.DC_ROUTE)))
    return lead


def _booked(person=s.DANA, *, route=s.DC_ROUTE, **order):
    lead = s._order(person, **order)
    trip = s._trip(lead, s.PLAIN_DAY, time(14, 0), route=route)
    return s._book(lead), trip


def _plan(lead) -> PaymentPlan:
    plan, _ = PaymentPlan.objects.get_or_create(lead=lead)
    plan.snapshot_total()
    return plan


def _receipt(trip, key: str, **fields) -> Charge:
    """A trip charge or refund that processing marked as a receipt just now."""
    return _plan(trip.lead).charges.create(
        reservation=trip,
        status=Charge.Status.SUCCEEDED,
        idempotency_key=f"golden-{key}",
        receipt_due_at=timezone.now(),
        **fields,
    )


# --- the clicks ------------------------------------------------------------------------


def _send_quote():
    result = lead_services.send_quote(_quotable(), base_url=s.BASE_URL)
    assert result.ok, result.error


def _send_quote_on_account():
    lead = _quotable()
    lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
    lead.save(update_fields=["billing_group"])
    result = lead_services.send_quote(lead, base_url=s.BASE_URL)
    assert result.ok, result.error


def _send_quote_by_text_only():
    result = lead_services.send_quote(_quotable(), base_url=s.BASE_URL, channels={"sms"})
    assert result.ok, result.error


def _pay_link_by_text():
    lead, _ = _booked()
    assert payments.send_pay_link(lead, base_url=s.BASE_URL, sender_name=AGENT) == "sms"


def _pay_link_by_email():
    lead, _ = _booked({**s.DANA, "phone": ""})
    assert payments.send_pay_link(lead, base_url=s.BASE_URL, sender_name=AGENT) == "email"


def _pay_link_for_a_trip():
    lead, trip = _booked()
    _plan(lead).charges.create(
        kind=Charge.Kind.TRIP,
        reservation=trip,
        amount=Decimal("185.00"),
        status=Charge.Status.PENDING,
        stripe_payment_intent_id="pi_golden_link",
        stripe_client_secret="pi_golden_link_secret",
        idempotency_key="golden-trip-link",
    )
    assert payments.send_pay_link(lead, base_url=s.BASE_URL, sender_name=AGENT) == "sms"


def _terms_link_by_text():
    lead, _ = _booked()
    assert payments.send_terms_link(lead, base_url=s.BASE_URL, sender_name=AGENT) == "sms"


def _terms_link_by_email():
    lead, _ = _booked({**s.DANA, "phone": ""})
    assert payments.send_terms_link(lead, base_url=s.BASE_URL, sender_name=AGENT) == "email"


def _receipt_for_overtime():
    _, trip = _booked()
    charge = _receipt(
        trip,
        "overtime",
        kind=Charge.Kind.TRIP,
        amount=Decimal("185.00"),
        receipt_lines=[["Overtime (1 hr)", "185.00"]],
        card_brand="visa",
        card_last4="4242",
    )
    assert receipts.send_receipt(charge.pk) is True


def _receipt_for_an_added_charge_paid_by_link():
    _, trip = _booked()
    charge = _receipt(
        trip,
        "added",
        kind=Charge.Kind.TRIP,
        amount=Decimal("80.00"),
        note="Cleaning",
        receipt_lines=[["Cleaning", "100.00"], ["Credit applied", "-20.00"]],
        stripe_client_secret="pi_golden_added_secret",
        card_brand="amex",
        card_last4="1005",
    )
    assert receipts.send_receipt(charge.pk) is True


def _refund_receipt_by_check_to_the_billing_contact():
    _, trip = _booked(billing=s.PAT)
    charge = _receipt(
        trip,
        "refund",
        kind=Charge.Kind.REFUND,
        amount=Decimal("40.00"),
        note="Late pickup",
        refund_method=Charge.RefundMethod.CHECK,
        reference="1042",
    )
    assert receipts.send_receipt(charge.pk) is True


def _affiliate_offer():
    _, trip = _booked(route=s.RECEPTION_ROUTE)
    vendor = VendorFactory(**s.VENDOR)
    dispatch_services.send_offer(trip, vendor, payout=Decimal("1200.00"))


def _affiliate_cancel():
    _, trip = _booked(route=s.RECEPTION_ROUTE)
    assignment = AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(**s.VENDOR),
        status=Assignment.Status.CONFIRMED,
        payout=Decimal("640.00"),
    )
    assert dispatch_services.send_cancellation(assignment) is True


CLICKS = {
    "send_quote": _send_quote,
    "send_quote_on_account": _send_quote_on_account,
    "send_quote_by_text_only": _send_quote_by_text_only,
    "pay_link_by_text": _pay_link_by_text,
    "pay_link_by_email": _pay_link_by_email,
    "pay_link_for_a_trip": _pay_link_for_a_trip,
    "terms_link_by_text": _terms_link_by_text,
    "terms_link_by_email": _terms_link_by_email,
    "receipt_for_overtime": _receipt_for_overtime,
    "receipt_for_an_added_charge_paid_by_link": _receipt_for_an_added_charge_paid_by_link,
    "refund_receipt_by_check_to_the_billing_contact": (
        _refund_receipt_by_check_to_the_billing_contact
    ),
    "affiliate_offer": _affiliate_offer,
    "affiliate_cancel": _affiliate_cancel,
}


# --- capture ---------------------------------------------------------------------------


def _text(value: str) -> str:
    """Whitespace-normalised, with what differs between runs (tokens, pks, numbers) named."""
    return " ".join(_RECEIPT_NO.sub("<RECEIPT>", s.normalise(value)).split())


def _outcome(name: str) -> list[dict]:
    """Click once at the scenario's moment; every send it made, normalised and sorted."""
    calls: list[dict] = []

    def send(*, identifier, body, channel_type="phone", **_):
        calls.append({"channel_type": channel_type, "identifier": identifier, "body": body})
        return {"uid": f"golden-{len(calls)}"}

    with s.frozen(), patch(SEND, side_effect=send):
        mailed = len(mail.outbox)
        CLICKS[name]()
        for message in mail.outbox[mailed:]:
            calls.append(
                {"email_to": list(message.to), "subject": message.subject, "body": message.body}
            )
        normalised = [
            {k: _text(v) if isinstance(v, str) else v for k, v in c.items()} for c in calls
        ]
    return sorted(normalised, key=lambda c: json.dumps(c, sort_keys=True))


def _expected() -> dict:
    return json.loads(EXPECTED.read_text()) if EXPECTED.exists() else {}


@pytest.mark.parametrize("name", sorted(CLICKS))
def test_staff_click_golden(name):
    got = _outcome(name)
    recorded = _expected()
    if RECORD and name not in recorded:
        # Recording only ever adds a new scenario; an existing entry is never rewritten.
        recorded[name] = got
        EXPECTED.write_text(
            json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return
    assert got == recorded[name]


@pytest.mark.skipif(RECORD, reason="recording")
def test_every_click_is_recorded():
    assert set(_expected()) == set(CLICKS)


@pytest.mark.skipif(RECORD, reason="recording")
def test_the_recording_is_real_copy():
    """Every click sent something; no blank body, no "None", no unrendered placeholder."""
    for name, calls in _expected().items():
        assert calls, name
        for call in calls:
            text = " ".join(str(v) for v in call.values())
            assert "None" not in text, name
            assert not re.search(r"\{\w+\}", text), name
            assert call["body"].strip(), name
