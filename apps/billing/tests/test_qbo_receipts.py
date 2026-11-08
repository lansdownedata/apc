"""APC-108 — QuickBooks card customers, Sales Receipts and Refund Receipts."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import qbo_receipts
from apps.billing.factories import BillingAccountFactory, CustomerLinkFactory
from apps.billing.invoices import line_description
from apps.billing.models import CustomerLink, QboAccountMapping, SyncState
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.integrations import qbo
from apps.notifications.models import Notification
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.payments.qbo_bills import _Refused
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import TripReview

pytestmark = pytest.mark.django_db


def test_customer_link_is_one_per_contact() -> None:
    link = CustomerLinkFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerLinkFactory(contact=link.contact)


def test_charge_has_quickbooks_fields() -> None:
    charge = ChargeFactory()
    assert charge.qbo_sync_state == SyncState.NOT_SYNCED
    assert charge.qbo_push_started_at is None


def test_mapping_has_deposit_account() -> None:
    assert QboAccountMapping.load().sales_deposit_account is None


# --- customers ---

REALM = "apps.billing.qbo_receipts.current_realm"
REQ = "apps.billing.qbo_receipts.qbo.request"


@pytest.fixture(autouse=True)
def realm():
    with (
        patch(REALM, return_value="123"),
        patch("apps.billing.qbo_sync.current_realm", return_value="123"),
        patch("apps.integrations.qbo.is_connected", return_value=True),
    ):
        yield


def _fault(code, message="", status=400, detail=""):
    error = {"code": code, "Message": message}
    if detail:
        error["Detail"] = detail
    return qbo.QboAPIError(status, json.dumps({"Fault": {"Error": [error]}}))


def _alerts():
    return Notification.objects.filter(kind=Notification.Kind.SYNC_FAILED)


def _customer(id_="55", token="0", name="Ada Lovelace", email=None):
    obj = {"Id": id_, "SyncToken": token, "DisplayName": name}
    if email is not None:
        obj["PrimaryEmailAddr"] = {"Address": email}
    return obj


def _link(name="Ada Lovelace", email="ada@example.com", phone="+15555550100", **kwargs):
    contact = ContactFactory(name=name, email=email, phone=phone)
    return CustomerLinkFactory(contact=contact, display_name=name, **kwargs)


def test_customer_payload_shape() -> None:
    link = _link()
    assert qbo_receipts.customer_payload(link) == {
        "DisplayName": "Ada Lovelace",
        "PrimaryEmailAddr": {"Address": "ada@example.com"},
        "PrimaryPhone": {"FreeFormNumber": "+15555550100"},
    }
    bare = _link(name="Grace Hopper", email=None, phone="")
    assert qbo_receipts.customer_payload(bare) == {"DisplayName": "Grace Hopper"}
    # On an update a removed email or number is cleared in QuickBooks, not kept.
    bare.qbo_id = "9"
    assert qbo_receipts.customer_payload(bare) == {
        "DisplayName": "Grace Hopper",
        "PrimaryEmailAddr": {"Address": ""},
        "PrimaryPhone": {"FreeFormNumber": ""},
    }


def test_customer_ref_uses_billing_account_when_no_link() -> None:
    account = BillingAccountFactory(qbo_id="77", qbo_realm_id="123")
    contact = account.contact
    assert qbo_receipts.customer_ref(contact, "123") == {"value": "77"}
    assert qbo_receipts.ensure_link(contact) is None
    assert not CustomerLink.objects.filter(contact=contact).exists()
    # An account in another company is no use here: the contact gets a link of its own.
    assert qbo_receipts.customer_ref(contact, "999") is None


def test_ensure_link_creates_one_for_a_card_contact() -> None:
    contact = ContactFactory(name="  " + "N" * 120 + "  ")
    link = qbo_receipts.ensure_link(contact)
    assert link.display_name == "N" * 100
    assert qbo_receipts.ensure_link(contact) == link
    assert CustomerLink.objects.filter(contact=contact).count() == 1
    assert qbo_receipts.customer_ref(contact, "123") is None


def test_customer_ref_prefers_pushed_link_over_later_account() -> None:
    link = _link(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    BillingAccountFactory(contact=link.contact, qbo_id="77", qbo_realm_id="123")
    assert qbo_receipts.customer_ref(link.contact, "123") == {"value": "55"}
    assert qbo_receipts.ensure_link(link.contact) == link


def test_push_customer_creates_with_requestid() -> None:
    link = _link()
    with patch(REQ, return_value={"Customer": _customer()}) as req:
        assert qbo_receipts.push_customer(link) is True
    args, kwargs = req.call_args
    assert args == ("POST", "customer")
    assert kwargs["params"]["requestid"] == f"apc-c{link.pk}-create"
    assert kwargs["json"]["DisplayName"] == "Ada Lovelace"
    link.refresh_from_db()
    assert link.qbo_sync_state == SyncState.SYNCED
    assert (link.qbo_id, link.qbo_sync_token, link.qbo_realm_id) == ("55", "0", "123")
    assert link.qbo_push_started_at is None


def test_duplicate_name_takeover_with_matching_email_links() -> None:
    link = _link(email="ada@example.com")
    found = {"QueryResponse": {"Customer": [_customer(email=" Ada@Example.COM ")]}}
    effects = [_fault("6240", "Duplicate Name Exists Error"), found, {"Customer": _customer()}]
    with patch(REQ, side_effect=effects) as req:
        assert qbo_receipts.push_customer(link) is True
    query = req.call_args_list[1].kwargs["params"]["query"]
    assert query == "select * from Customer where DisplayName = 'Ada Lovelace'"
    update = req.call_args_list[2]
    assert update.args == ("POST", "customer")
    assert update.kwargs["json"] == {
        "DisplayName": "Ada Lovelace",
        "PrimaryEmailAddr": {"Address": "ada@example.com"},
        "PrimaryPhone": {"FreeFormNumber": "+15555550100"},
        "Id": "55",
        "SyncToken": "0",
        "sparse": True,
    }
    link.refresh_from_db()
    assert (link.qbo_id, link.qbo_sync_state) == ("55", SyncState.SYNCED)


def test_duplicate_name_takeover_when_both_emails_blank() -> None:
    link = _link(email=None, phone="")
    found = {"QueryResponse": {"Customer": [_customer()]}}
    effects = [_fault("6240"), found, {"Customer": _customer()}]
    with patch(REQ, side_effect=effects) as req:
        assert qbo_receipts.push_customer(link) is True
    # Never a blank over what the bookkeeper has.
    assert req.call_args_list[2].kwargs["json"] == {
        "DisplayName": "Ada Lovelace",
        "Id": "55",
        "SyncToken": "0",
        "sparse": True,
    }


REFUSAL = (
    "QuickBooks already has a name like “Ada Lovelace” (it may differ in case or spacing)."
    " Rename the customer here or there, then retry."
)


@pytest.mark.parametrize(
    "ours, theirs",
    [("ada@example.com", "someone@else.com"), ("ada@example.com", None), (None, "x@y.com")],
)
def test_duplicate_name_takeover_requires_matching_email(ours, theirs) -> None:
    link = _link(email=ours)
    found = {"QueryResponse": {"Customer": [_customer(email=theirs)]}}
    with patch(REQ, side_effect=[_fault("6240"), found]) as req:
        assert qbo_receipts.push_customer(link) is False
        assert req.call_count == 2  # nothing is sent over the other person's customer
    link.refresh_from_db()
    assert link.qbo_id == ""
    assert link.qbo_sync_state == SyncState.ERROR
    assert link.qbo_sync_error == REFUSAL
    assert _alerts().count() == 1
    assert _alerts().get().title == "QuickBooks customer failed: Ada Lovelace"
    # The hourly retry doesn't alert again.
    with patch(REQ, side_effect=[_fault("6240"), found]):
        assert qbo_receipts.push_customer(link) is False
    assert _alerts().count() == 1


@pytest.mark.parametrize("matches", [0, 2])
def test_duplicate_name_with_two_matches_refuses(matches) -> None:
    link = _link()
    rows = [_customer(id_=str(n), email="ada@example.com") for n in range(matches)]
    with patch(REQ, side_effect=[_fault("6240"), {"QueryResponse": {"Customer": rows}}]):
        assert qbo_receipts.push_customer(link) is False
    link.refresh_from_db()
    assert (link.qbo_id, link.qbo_sync_error) == ("", REFUSAL)


def test_other_refusal_is_not_a_takeover() -> None:
    link = _link()
    refusal = _fault("6000", "A business validation error has occurred", detail="Bad email")
    with patch(REQ, side_effect=[refusal]) as req:
        assert qbo_receipts.push_customer(link) is False
    assert req.call_count == 1
    link.refresh_from_db()
    assert link.qbo_sync_error == "QuickBooks didn't accept this customer: Bad email"
    assert link.qbo_create_attempt == 1


def test_push_customer_refuses_while_leased() -> None:
    link = _link()
    CustomerLink.objects.filter(pk=link.pk).update(qbo_push_started_at=timezone.now())
    with patch(REQ) as req:
        assert qbo_receipts.push_customer(link) is False
    req.assert_not_called()
    # A lease from a push that died is taken over.
    stale = timezone.now() - timedelta(minutes=11)
    CustomerLink.objects.filter(pk=link.pk).update(qbo_push_started_at=stale)
    with patch(REQ, return_value={"Customer": _customer()}):
        assert qbo_receipts.push_customer(link) is True


def _candidate(contact, *, kind=Charge.Kind.TRIP, status=Charge.Status.SUCCEEDED, processed=True):
    trip = ReservationFactory(lead__contact=contact)
    TripReview.objects.create(reservation=trip, processed_at=timezone.now() if processed else None)
    plan = PaymentPlanFactory(lead=trip.lead)
    return ChargeFactory(plan=plan, reservation=trip, kind=kind, status=status)


def test_pending_links_only_with_receipt_candidates() -> None:
    synced = {"qbo_sync_state": SyncState.SYNCED}
    trip_charge = _link(email=None, name="Trip")
    _candidate(trip_charge.contact)
    refund = _link(email=None, name="Refund")
    _candidate(refund.contact, kind=Charge.Kind.REFUND)
    deposit_only = _link(email=None, name="Deposit")
    _candidate(deposit_only.contact, kind=Charge.Kind.DEPOSIT)
    failed = _link(email=None, name="Failed")
    _candidate(failed.contact, status=Charge.Status.FAILED)
    unprocessed = _link(email=None, name="Unprocessed")
    _candidate(unprocessed.contact, processed=False)
    no_trip = _link(email=None, name="No trip")
    ChargeFactory(
        plan__lead__contact=no_trip.contact, kind=Charge.Kind.TRIP, status=Charge.Status.SUCCEEDED
    )
    in_step = _link(email=None, name="In step", qbo_id="5", qbo_realm_id="123", **synced)
    _candidate(in_step.contact)
    refused = _link(email=None, name="Refused", qbo_sync_state=SyncState.ERROR)
    _candidate(refused.contact)
    elsewhere = _link(email=None, name="Elsewhere", qbo_id="5", qbo_realm_id="999", **synced)
    _candidate(elsewhere.contact)
    edited = _link(email=None, name="Edited", qbo_id="6", qbo_realm_id="123")
    _candidate(edited.contact)
    BillingAccountFactory(contact=edited.contact, qbo_id="77", qbo_realm_id="123")
    # Not pushed, and the contact's billing account is in QuickBooks: that is its customer.
    covered = _link(email=None, name="Covered")
    _candidate(covered.contact)
    BillingAccountFactory(contact=covered.contact, qbo_id="78", qbo_realm_id="123")

    names = list(qbo_receipts.pending_links("123").values_list("display_name", flat=True))
    assert names == ["Trip", "Refund", "Refused", "Elsewhere", "Edited"]


def test_contact_edit_requeues_the_link(client) -> None:
    link = _link(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[link.contact.pk]), {"notes": "VIP"})
    assert resp.status_code == 200
    link.refresh_from_db()
    assert link.qbo_sync_state == SyncState.SYNCED  # nothing QuickBooks holds changed

    resp = client.post(reverse("contact_update", args=[link.contact.pk]), {"name": " Ada King "})
    assert resp.status_code == 200
    link.refresh_from_db()
    assert (link.display_name, link.qbo_sync_state) == ("Ada King", SyncState.NOT_SYNCED)

    CustomerLink.objects.filter(pk=link.pk).update(qbo_sync_state=SyncState.SYNCED)
    client.post(reverse("contact_update", args=[link.contact.pk]), {"email": "ada@king.org"})
    link.refresh_from_db()
    assert link.qbo_sync_state == SyncState.NOT_SYNCED

    CustomerLink.objects.filter(pk=link.pk).update(qbo_sync_state=SyncState.SYNCED)
    client.post(
        reverse("contact_phone_add", args=[link.contact.pk]),
        {"number": "+12025550123", "texting": "true"},
    )
    link.refresh_from_db()
    assert link.qbo_sync_state == SyncState.NOT_SYNCED


def test_contact_edit_without_a_link_creates_none(client) -> None:
    contact = ContactFactory(name="Ada")
    client.force_login(UserFactory())
    client.post(reverse("contact_update", args=[contact.pk]), {"name": "Ada King"})
    assert not CustomerLink.objects.exists()


def test_takeover_refuses_a_customer_already_linked_here() -> None:
    first = _link(email=None, phone="")
    with patch(REQ, return_value={"Customer": _customer()}):
        assert qbo_receipts.push_customer(first) is True
    second = _link(email=None, phone="")
    found = {"QueryResponse": {"Customer": [_customer()]}}
    with patch(REQ, side_effect=[_fault("6240"), found]) as req:
        assert qbo_receipts.push_customer(second) is False
    assert req.call_count == 2
    second.refresh_from_db()
    assert (second.qbo_id, second.qbo_sync_state) == ("", SyncState.ERROR)
    assert second.qbo_sync_error == REFUSAL
    first.refresh_from_db()
    assert (first.qbo_id, first.qbo_sync_state) == ("55", SyncState.SYNCED)


@pytest.mark.parametrize("holder", ["account", "group"])
def test_takeover_refuses_a_customer_held_by_billing(holder) -> None:
    from apps.billing.factories import AccountGroupFactory

    if holder == "account":
        BillingAccountFactory(qbo_id="55", qbo_realm_id="123")
    else:
        AccountGroupFactory(qbo_id="55", qbo_realm_id="123")
    link = _link()
    found = {"QueryResponse": {"Customer": [_customer(email="ada@example.com")]}}
    with patch(REQ, side_effect=[_fault("6240"), found]):
        assert qbo_receipts.push_customer(link) is False
    link.refresh_from_db()
    assert (link.qbo_id, link.qbo_sync_error) == ("", REFUSAL)


def test_takeover_allows_a_customer_held_in_another_company() -> None:
    _link(name="Other", email=None, qbo_id="55", qbo_realm_id="999")
    link = _link()
    found = {"QueryResponse": {"Customer": [_customer(email="ada@example.com")]}}
    with patch(REQ, side_effect=[_fault("6240"), found, {"Customer": _customer()}]):
        assert qbo_receipts.push_customer(link) is True


@pytest.mark.parametrize(
    "ours, theirs, linked",
    [
        ("+1 (555) 555-0100", "15555550100", True),
        ("+15555550100", "+15555550199", False),
        ("+15555550100", None, False),
        ("", "+15555550100", False),
    ],
)
def test_both_blank_emails_require_matching_phones(ours, theirs, linked) -> None:
    link = _link(email=None, phone=ours)
    customer = _customer()
    if theirs is not None:
        customer["PrimaryPhone"] = {"FreeFormNumber": theirs}
    found = {"QueryResponse": {"Customer": [customer]}}
    with patch(REQ, side_effect=[_fault("6240"), found, {"Customer": _customer()}]):
        assert qbo_receipts.push_customer(link) is linked
    link.refresh_from_db()
    assert link.qbo_sync_error == ("" if linked else REFUSAL)


def test_booking_intake_rename_requeues_the_link() -> None:
    link = _link(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    found = Contact.objects.match_or_create(
        name="Ada King", phone="", email="ada@example.com", channel=link.contact.channel
    )
    assert found == link.contact
    link.refresh_from_db()
    assert (link.display_name, link.qbo_sync_state) == ("Ada King", SyncState.NOT_SYNCED)


def test_push_customer_skips_when_the_account_is_the_customer() -> None:
    link = _link()
    BillingAccountFactory(contact=link.contact, qbo_id="77", qbo_realm_id="123")
    with patch(REQ) as req:
        assert qbo_receipts.push_customer(link) is False
    req.assert_not_called()
    # Pushed here first: it stays the customer and its edits still go.
    CustomerLink.objects.filter(pk=link.pk).update(qbo_id="55", qbo_realm_id="123")
    with patch(REQ, return_value={"Customer": _customer()}):
        assert qbo_receipts.push_customer(link) is True


# --- payloads ---

DEPOSIT = {"value": "35"}


@pytest.fixture
def receipt_mapping():
    with (
        patch("apps.billing.qbo_invoices.current_realm", return_value="123"),
    ):
        yield _receipt_mapping()


def _receipt_mapping():
    from apps.billing.factories import QboItemFactory
    from apps.billing.models import QboAccount

    config = QboAccountMapping.load()
    config.trip_item = QboItemFactory(realm_id="123", qbo_id="9")
    config.sales_deposit_account = QboAccount.objects.create(
        realm_id="123", qbo_id="35", name="Undeposited Funds", account_type="Other Current Asset"
    )
    config.save()
    return config


def _card_charge(*, kind=Charge.Kind.TRIP, **kwargs):
    from apps.billing.tests._helpers import _processed_trip

    trip = _processed_trip(accountant=UserFactory())
    CustomerLinkFactory(
        contact=trip.lead.contact,
        qbo_id="77",
        qbo_realm_id="123",
        qbo_sync_state=SyncState.SYNCED,
    )
    defaults = {
        "kind": kind,
        "status": Charge.Status.SUCCEEDED,
        "reservation": trip,
        "amount": Decimal("600.00"),
        "stripe_payment_intent_id": "pi_123",
        "card_last4": "4242",
    }
    return ChargeFactory(**{**defaults, **kwargs})


def test_sales_receipt_payload_exact(receipt_mapping) -> None:
    charge = _card_charge()
    trip = charge.reservation
    body = qbo_receipts.sales_receipt_payload(charge, "123")
    assert body == {
        "CustomerRef": {"value": "77"},
        "TxnDate": qbo_receipts.txn_date(charge).isoformat(),
        "DepositToAccountRef": DEPOSIT,
        "PaymentRefNum": "pi_123",
        "PrivateNote": f"{trip.reference} · order {trip.lead.quote_no} · receipt R-{charge.pk}",
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": "600.00",
                "Description": f"{line_description(trip)} · card ••4242 · receipt R-{charge.pk}",
                "SalesItemLineDetail": {"ItemRef": {"value": "9"}, "Qty": 1, "UnitPrice": "600.00"},
            }
        ],
    }
    assert "PaymentMethodRef" not in body
    assert "DocNumber" not in body


def test_refund_receipt_payload_exact(receipt_mapping) -> None:
    charge = _card_charge(
        kind=Charge.Kind.REFUND,
        amount=Decimal("100.00"),
        note="Trip cancelled",
        stripe_payment_intent_id="",
        stripe_refund_id="re_" + "x" * 30,
    )
    trip = charge.reservation
    assert qbo_receipts.refund_receipt_payload(charge, "123") == {
        "CustomerRef": {"value": "77"},
        "TxnDate": qbo_receipts.txn_date(charge).isoformat(),
        "DepositToAccountRef": DEPOSIT,
        "PaymentRefNum": ("re_" + "x" * 30)[:21],
        "PrivateNote": f"{trip.reference} · order {trip.lead.quote_no} · receipt R-{charge.pk}",
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": "100.00",
                "Description": f"Refund · Trip cancelled · {trip.reference}",
                "SalesItemLineDetail": {"ItemRef": {"value": "9"}, "Qty": 1, "UnitPrice": "100.00"},
            }
        ],
    }


def test_txn_date_is_the_trip_local_date() -> None:
    charge = _card_charge()
    charge.created_at = datetime(2026, 3, 2, 4, 30, tzinfo=UTC)
    charge.reservation.pickup_timezone = "America/New_York"
    assert qbo_receipts.txn_date(charge) == date(2026, 3, 1)


def test_card_segment_omitted_when_last4_blank(receipt_mapping) -> None:
    charge = _card_charge(card_last4="")
    line = qbo_receipts.sales_receipt_payload(charge, "123")["Line"][0]
    assert "card" not in line["Description"]
    assert line["Description"].endswith(f" · receipt R-{charge.pk}")


def test_payment_ref_omitted_when_blank(receipt_mapping) -> None:
    charge = _card_charge(stripe_payment_intent_id="")
    assert "PaymentRefNum" not in qbo_receipts.sales_receipt_payload(charge, "123")


@pytest.mark.parametrize("missing", ["item", "account", "customer"])
def test_payload_refuses_without_item_account_or_customer(receipt_mapping, missing) -> None:
    charge = _card_charge()
    if missing == "item":
        receipt_mapping.trip_item = None
        receipt_mapping.save()
    elif missing == "account":
        receipt_mapping.sales_deposit_account = None
        receipt_mapping.save()
    else:
        CustomerLink.objects.all().delete()
        charge = Charge.objects.get(pk=charge.pk)  # not the copy that still holds the link
    for build in (qbo_receipts.sales_receipt_payload, qbo_receipts.refund_receipt_payload):
        with pytest.raises(_Refused) as caught:
            build(charge, "123")
        assert caught.value.alert is False


def test_receipt_matches_ignores_subtotal_line() -> None:
    body = {"Line": [{"Amount": "600.00"}]}
    obj = {
        "TotalAmt": 600,
        "Line": [
            {"DetailType": "SalesItemLineDetail"},
            {"DetailType": "SubTotalLineDetail"},
        ],
    }
    assert qbo_receipts.receipt_matches(obj, body)
    obj["Line"].append({"DetailType": "SalesItemLineDetail"})
    assert not qbo_receipts.receipt_matches(obj, body)
    assert not qbo_receipts.receipt_matches({**obj, "TotalAmt": 599}, body)


# --- the push and the hourly run ---

START = timezone.localdate() - timedelta(days=30)


@pytest.fixture
def ready(receipt_mapping):
    receipt_mapping.customer_docs_from = START
    receipt_mapping.save()
    return receipt_mapping


def _item_lines(amount):
    return [
        {"DetailType": "SalesItemLineDetail", "Amount": amount},
        {"DetailType": "SubTotalLineDetail", "Amount": amount},
    ]


def _receipt(key="SalesReceipt", id_="900", total=600, token="0"):
    return {
        key: {
            "Id": id_,
            "SyncToken": token,
            "TotalAmt": total,
            "DocNumber": "DOC-7731",
            "Line": _item_lines(total),
        }
    }


def _refund(**kwargs):
    defaults = {
        "kind": Charge.Kind.REFUND,
        "amount": Decimal("100.00"),
        "stripe_payment_intent_id": "",
        "stripe_refund_id": "re_1",
        "refund_method": Charge.RefundMethod.CARD,
    }
    return _card_charge(**{**defaults, **kwargs})


def _paths(req):
    return [c.args[1] for c in req.call_args_list]


def test_push_creates_sales_receipt_and_links(ready) -> None:
    charge = _card_charge()
    with patch(REQ, return_value=_receipt()) as req:
        assert qbo_receipts.sync_pending() == 1
    args, kwargs = req.call_args
    assert args == ("POST", "salesreceipt")
    assert kwargs["params"]["requestid"] == f"apc-s{charge.pk}-create"
    assert "DocNumber" not in kwargs["json"]
    charge.refresh_from_db()
    assert charge.qbo_sync_state == SyncState.SYNCED
    assert (charge.qbo_id, charge.qbo_sync_token, charge.qbo_realm_id) == ("900", "0", "123")
    assert charge.qbo_push_started_at is None
    # QuickBooks numbers the receipt; that number is never kept here.
    assert "DOC-7731" not in str(Charge.objects.filter(pk=charge.pk).values().get())


def test_refund_pushes_refundreceipt(ready) -> None:
    refund = _refund(refund_method="")  # blank on older rows = the card
    with patch(REQ, return_value=_receipt("RefundReceipt", total=100)) as req:
        assert qbo_receipts.push_receipt(refund) is True
    args, kwargs = req.call_args
    assert args == ("POST", "refundreceipt")
    assert kwargs["params"]["requestid"] == f"apc-r{refund.pk}-create"
    refund.refresh_from_db()
    assert (refund.qbo_id, refund.qbo_sync_state) == ("900", SyncState.SYNCED)


@pytest.mark.parametrize("kind", [Charge.Kind.DEPOSIT, Charge.Kind.BALANCE, Charge.Kind.FINAL])
def test_deposit_and_balance_never_pushed(ready, kind) -> None:
    charge = _card_charge(kind=kind)
    assert qbo_receipts.pending_receipts("123", START) == []
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
        assert qbo_receipts.push_receipt(charge) is False
    req.assert_not_called()


def test_offline_refund_never_pushed(ready) -> None:
    refund = _refund(refund_method=Charge.RefundMethod.ZELLE)
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
        assert qbo_receipts.push_receipt(refund) is False
    req.assert_not_called()
    refund.refresh_from_db()
    assert (refund.qbo_sync_state, refund.qbo_sync_error) == (SyncState.NOT_SYNCED, "")


def _processed_on(charge, when) -> None:
    TripReview.objects.filter(reservation=charge.reservation).update(processed_at=when)


def test_historical_charge_before_start_left_alone(ready) -> None:
    charge = _card_charge()
    _processed_on(charge, timezone.now() - timedelta(days=60))
    assert qbo_receipts.pending_receipts("123", START) == []
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
    req.assert_not_called()
    charge.refresh_from_db()
    assert (charge.qbo_sync_state, charge.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert not _alerts().exists()
    assert qbo_receipts.sync_state(charge, connected=True)["before_start"] is True


def test_window_follows_the_trip_calendar(ready) -> None:
    # 02:00 UTC on the start date is still the day before in New York: not sent.
    charge = _card_charge()
    early = datetime.combine(START, datetime.min.time(), tzinfo=UTC) + timedelta(hours=2)
    _processed_on(charge, early)
    assert qbo_receipts.pending_receipts("123", START) == []
    _processed_on(charge, early + timedelta(hours=4))
    assert [c.pk for c in qbo_receipts.pending_receipts("123", START)] == [charge.pk]


def test_window_and_blocks_alert_once_and_clear(ready) -> None:
    historical = _card_charge()
    _processed_on(historical, timezone.now() - timedelta(days=60))
    charge = _card_charge()
    ready.customer_docs_from = None
    ready.save()
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
        assert qbo_receipts.sync_pending() == 0
    req.assert_not_called()
    for row in (historical, charge):
        row.refresh_from_db()
        assert (row.qbo_sync_state, row.qbo_sync_error) == (
            SyncState.ERROR,
            qbo_receipts.NO_START_DATE,
        )
    assert _alerts().count() == 1
    assert _alerts().get().title == "Card receipts can't go to QuickBooks yet"
    assert _alerts().get().detail == qbo_receipts.NO_START_DATE

    account = ready.sales_deposit_account
    ready.customer_docs_from = START
    ready.sales_deposit_account = None
    ready.save()
    with patch(REQ) as req:
        qbo_receipts.sync_pending()
        qbo_receipts.sync_pending()
    req.assert_not_called()
    charge.refresh_from_db()
    assert charge.qbo_sync_error == qbo_receipts.NO_DEPOSIT_ACCOUNT
    historical.refresh_from_db()
    # The start date it waited on is set and it falls before it: never sent, nothing to say.
    assert (historical.qbo_sync_state, historical.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert _alerts().count() == 2

    ready.sales_deposit_account = account
    ready.save()
    with patch(REQ, return_value=_receipt()) as req:
        assert qbo_receipts.sync_pending() == 1
    assert _paths(req) == ["salesreceipt"]
    charge.refresh_from_db()
    assert (charge.qbo_sync_state, charge.qbo_sync_error) == (SyncState.SYNCED, "")
    assert _alerts().count() == 2


def test_missing_item_blocks_once(ready) -> None:
    charge = _card_charge()
    ready.trip_item = None
    ready.save()
    with patch(REQ) as req:
        qbo_receipts.sync_pending()
        qbo_receipts.sync_pending()
    req.assert_not_called()
    charge.refresh_from_db()
    assert charge.qbo_sync_error == qbo_receipts.NO_TRIP_ITEM
    assert _alerts().count() == 1


def test_waits_for_its_customer(ready) -> None:
    charge = _card_charge()
    contact = charge.reservation.lead.contact
    CustomerLink.objects.filter(contact=contact).delete()
    customer = {"Customer": _customer(id_="61", name=contact.name)}
    with patch(REQ, side_effect=[customer, _receipt()]) as req:
        assert qbo_receipts.sync_pending() == 2
    assert _paths(req) == ["customer", "salesreceipt"]
    link = CustomerLink.objects.get(contact=contact)
    assert req.call_args_list[0].kwargs["params"]["requestid"] == f"apc-c{link.pk}-create"
    assert req.call_args_list[1].kwargs["json"]["CustomerRef"] == {"value": "61"}
    charge.refresh_from_db()
    assert charge.qbo_sync_state == SyncState.SYNCED

    # A customer QuickBooks refuses: the receipt waits, with nothing said on it.
    other = _card_charge()
    CustomerLink.objects.filter(contact=other.reservation.lead.contact).delete()
    refusal = _fault("6000", "A business validation error has occurred", detail="Bad email")
    with patch(REQ, side_effect=[refusal]) as req:
        assert qbo_receipts.sync_pending() == 0
    assert _paths(req) == ["customer"]
    other.refresh_from_db()
    assert (other.qbo_sync_state, other.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    with patch(REQ) as req:
        assert qbo_receipts.push_receipt(other) is False
    req.assert_not_called()
    assert _alerts().count() == 1  # the customer's, once


def test_one_link_per_contact_with_many_charges(ready) -> None:
    first = _card_charge()
    contact = first.reservation.lead.contact
    CustomerLink.objects.filter(contact=contact).delete()
    ChargeFactory(
        kind=Charge.Kind.TRIP,
        status=Charge.Status.SUCCEEDED,
        reservation=first.reservation,
        amount=Decimal("50.00"),
    )
    customer = {"Customer": _customer(id_="61", name=contact.name)}
    with patch(REQ, side_effect=[customer, _receipt(), _receipt(id_="901", total=50)]) as req:
        assert qbo_receipts.sync_pending() == 3
    assert _paths(req) == ["customer", "salesreceipt", "salesreceipt"]
    assert CustomerLink.objects.filter(contact=contact).count() == 1


def test_billing_account_customer_needs_no_link(ready) -> None:
    charge = _card_charge()
    contact = charge.reservation.lead.contact
    CustomerLink.objects.filter(contact=contact).delete()
    contact.billing_accounts.update(qbo_id="77", qbo_realm_id="123")
    with patch(REQ, return_value=_receipt()) as req:
        assert qbo_receipts.sync_pending() == 1
    assert _paths(req) == ["salesreceipt"]
    assert req.call_args.kwargs["json"]["CustomerRef"] == {"value": "77"}
    assert not CustomerLink.objects.exists()


def test_mismatch_marks_error_once(ready) -> None:
    charge = _card_charge()
    with patch(REQ, return_value=_receipt(total=650)):
        assert qbo_receipts.sync_pending() == 0
    charge.refresh_from_db()
    assert (charge.qbo_id, charge.qbo_sync_state) == ("900", SyncState.ERROR)
    assert charge.qbo_sync_error == (
        "QuickBooks totals this receipt $650.00, not $600.00. "
        "Check sales tax on the Product/Service."
    )
    assert _alerts().count() == 1
    name = charge.reservation.lead.contact.name
    assert _alerts().get().title == f"QuickBooks receipt failed: R-{charge.pk} {name}"
    assert _alerts().get().lead == charge.reservation.lead
    # The hourly run never sends it again; a person's Retry does, without a second alert.
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
    req.assert_not_called()
    with patch(REQ, return_value=_receipt(total=650, token="1")):
        assert qbo_receipts.push_receipt(charge) is False
    assert _alerts().count() == 1


def test_line_count_mismatch_sentence(ready) -> None:
    refund = _refund()
    answer = _receipt("RefundReceipt", total=100)
    answer["RefundReceipt"]["Line"].append({"DetailType": "SalesItemLineDetail"})
    with patch(REQ, return_value=answer):
        assert qbo_receipts.push_receipt(refund) is False
    refund.refresh_from_db()
    assert refund.qbo_sync_error == (
        "QuickBooks holds this refund receipt with 2 lines, not 1. Check it in QuickBooks."
    )
    name = refund.reservation.lead.contact.name
    assert _alerts().get().title == f"QuickBooks refund receipt failed: R-{refund.pk} {name}"


def test_refusal_alerts_once(ready) -> None:
    charge = _card_charge()
    refusal = _fault("6000", "A business validation error has occurred", detail="Inactive")
    with patch(REQ, side_effect=[refusal, refusal]) as req:
        assert qbo_receipts.sync_pending() == 0
        assert qbo_receipts.sync_pending() == 0
    assert req.call_count == 2  # never linked: the hourly run tries again
    charge.refresh_from_db()
    assert charge.qbo_sync_error == "QuickBooks didn't accept this receipt: Inactive"
    assert _alerts().count() == 1


def test_hourly_never_updates_a_linked_receipt(ready) -> None:
    charge = _card_charge(qbo_id="900", qbo_sync_token="2", qbo_realm_id="123")
    assert charge.qbo_sync_state == SyncState.NOT_SYNCED
    with patch(REQ) as req:
        assert qbo_receipts.sync_pending() == 0
    req.assert_not_called()
    # A person's Retry updates the one QuickBooks has.
    with patch(REQ, return_value=_receipt(token="3")) as req:
        assert qbo_receipts.push_receipt(charge) is True
    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["SyncToken"]) == ("900", "2")
    # Linked to another company: it goes to this one as new.
    elsewhere = _card_charge(qbo_id="5", qbo_realm_id="999", qbo_sync_state=SyncState.SYNCED)
    with patch(REQ, return_value=_receipt(id_="901")) as req:
        assert qbo_receipts.sync_pending() == 1
    assert "Id" not in req.call_args.kwargs["json"]
    elsewhere.refresh_from_db()
    assert (elsewhere.qbo_id, elsewhere.qbo_realm_id) == ("901", "123")


def test_refund_receipt_does_not_wait_for_the_charge(ready) -> None:
    charge = _card_charge()
    refund = _refund(reservation=charge.reservation)
    refusal = _fault("6000", "A business validation error has occurred", detail="Inactive")
    with patch(REQ, side_effect=[refusal, _receipt("RefundReceipt", total=100)]) as req:
        assert qbo_receipts.sync_pending() == 1
    assert _paths(req) == ["salesreceipt", "refundreceipt"]
    charge.refresh_from_db()
    refund.refresh_from_db()
    assert (charge.qbo_sync_state, refund.qbo_sync_state) == (SyncState.ERROR, SyncState.SYNCED)


def test_refund_receipt_pushes_with_its_charge_unsent(ready) -> None:
    charge = _card_charge(status=Charge.Status.PENDING)  # its charge isn't a receipt at all
    refund = _refund(reservation=charge.reservation)
    with patch(REQ, return_value=_receipt("RefundReceipt", total=100)) as req:
        assert qbo_receipts.sync_pending() == 1
    assert _paths(req) == ["refundreceipt"]
    refund.refresh_from_db()
    assert refund.qbo_sync_state == SyncState.SYNCED


def test_reprocessing_pushes_a_second_receipt_not_an_update(ready) -> None:
    first = _card_charge(
        qbo_id="900", qbo_sync_token="0", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED
    )
    trip = first.reservation
    # Unlocked and processed again: the difference is a new TRIP charge on the same trip.
    TripReview.objects.filter(reservation=trip).update(processed_at=timezone.now())
    second = ChargeFactory(
        kind=Charge.Kind.TRIP,
        status=Charge.Status.SUCCEEDED,
        reservation=trip,
        amount=Decimal("75.00"),
    )
    with patch(REQ, return_value=_receipt(id_="901", total=75)) as req:
        assert qbo_receipts.sync_pending() == 1
    kwargs = req.call_args.kwargs
    assert kwargs["params"]["requestid"] == f"apc-s{second.pk}-create"
    assert "Id" not in kwargs["json"]
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.qbo_id, first.qbo_sync_state) == ("900", SyncState.SYNCED)
    assert (second.qbo_id, second.qbo_sync_state) == ("901", SyncState.SYNCED)


def test_run_hourly_isolated_passes() -> None:
    from apps.billing import qbo_invoices
    from apps.payments import qbo_bills

    with (
        patch("apps.billing.qbo_sync.run_hourly", return_value=0),
        patch.object(qbo_bills, "refresh_accounts"),
        patch.object(qbo_invoices, "refresh_items"),
        patch.object(qbo_bills, "sync_pending", return_value=0),
        patch.object(qbo_receipts, "sync_pending", side_effect=RuntimeError("boom")),
        patch.object(qbo_invoices, "sync_pending", return_value=1) as invoices,
    ):
        assert qbo_bills.run_hourly() == 1
    invoices.assert_called_once()

    with (
        patch("apps.billing.qbo_sync.run_hourly", return_value=0),
        patch.object(qbo_bills, "refresh_accounts"),
        patch.object(qbo_invoices, "refresh_items"),
        patch.object(qbo_bills, "sync_pending", return_value=0),
        patch.object(qbo_receipts, "sync_pending", return_value=2),
        patch.object(qbo_invoices, "sync_pending", side_effect=RuntimeError("boom")),
    ):
        assert qbo_bills.run_hourly() == 2


def test_lease_refuses_concurrent_push(ready) -> None:
    charge = _card_charge()
    Charge.objects.filter(pk=charge.pk).update(qbo_push_started_at=timezone.now())
    with patch(REQ) as req:
        assert qbo_receipts.push_receipt(charge) is False
        assert qbo_receipts.sync_pending() == 0
    req.assert_not_called()
    # A lease from a push that died is taken over.
    stale = timezone.now() - timedelta(minutes=11)
    Charge.objects.filter(pk=charge.pk).update(qbo_push_started_at=stale)
    with patch(REQ, return_value=_receipt()):
        assert qbo_receipts.push_receipt(charge) is True
    charge.refresh_from_db()
    assert charge.qbo_push_started_at is None


def test_outage_pauses_and_marks_nothing(ready) -> None:
    charge = _card_charge()
    with patch(REQ, side_effect=[qbo.QboAPIError(503, "down")]):
        assert qbo_receipts.sync_pending() == 0
    charge.refresh_from_db()
    assert (charge.qbo_sync_state, charge.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert not _alerts().exists()


def test_bad_timezone_refuses_the_record_not_the_run(ready) -> None:
    from apps.reservations.models import Reservation

    bad = _card_charge()
    Reservation.objects.filter(pk=bad.reservation_id).update(pickup_timezone="Mars/Olympus")
    good = _card_charge()
    with patch(REQ, return_value=_receipt()) as req:
        assert qbo_receipts.sync_pending() == 1
    assert req.call_count == 1
    bad.refresh_from_db()
    good.refresh_from_db()
    assert good.qbo_sync_state == SyncState.SYNCED
    assert (bad.qbo_sync_state, bad.qbo_sync_error) == (
        SyncState.ERROR,
        "This trip's timezone isn't valid: Mars/Olympus.",
    )
    assert _alerts().count() == 1
    with patch(REQ) as req:
        qbo_receipts.sync_pending()
    req.assert_not_called()
    assert _alerts().count() == 1


def test_pending_receipts_query_is_flat(ready) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def build_all() -> int:
        # The two settings reads are once per receipt whatever the data; this guards the
        # per-charge relations (trip, order, contact, customer, vehicle, stops, review).
        with (
            patch.object(qbo_receipts, "trip_item_ref", return_value={"value": "9"}),
            patch.object(qbo_receipts, "deposit_account_ref", return_value=DEPOSIT),
            CaptureQueriesContext(connection) as queries,
        ):
            rows = qbo_receipts.pending_receipts("123", START)
            for charge in rows:
                build = (
                    qbo_receipts.sales_receipt_payload
                    if charge.kind == Charge.Kind.TRIP
                    else qbo_receipts.refund_receipt_payload
                )
                build(charge, "123")
        return len(rows), len(queries)

    from apps.billing.tests._helpers import _processed_trip

    first = _card_charge()
    one = build_all()
    # Three contacts: a pushed link, a pushed link, and a billing account's customer.
    leads = [first.reservation.lead]
    for n in range(2):
        lead = _processed_trip(accountant=UserFactory()).lead
        if n == 0:
            CustomerLinkFactory(
                contact=lead.contact,
                qbo_id="79",
                qbo_realm_id="123",
                qbo_sync_state=SyncState.SYNCED,
            )
        else:
            lead.contact.billing_accounts.update(qbo_id="78", qbo_realm_id="123")
        leads.append(lead)
    for n in range(9):
        trip = _processed_trip(accountant=UserFactory(), lead=leads[n % 3])
        ChargeFactory(
            kind=Charge.Kind.REFUND if n % 2 else Charge.Kind.TRIP,
            status=Charge.Status.SUCCEEDED,
            reservation=trip,
            amount=Decimal("10.00"),
        )
    ten = build_all()
    assert (one[0], ten[0]) == (1, 10)
    assert ten[1] == one[1]


def test_sync_state_none_when_disconnected() -> None:
    charge = ChargeFactory()
    assert qbo_receipts.sync_state(charge, connected=False) is None
    with patch("apps.integrations.qbo.is_connected", return_value=False):
        assert qbo_receipts.sync_state(charge) is None
        assert qbo_receipts.sync_pending() == 0


def test_sync_state_when_connected(ready) -> None:
    charge = _card_charge(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Nope")
    state = qbo_receipts.sync_state(charge, connected=True)
    assert state == {
        "state": SyncState.ERROR,
        "error": "Nope",
        "sync_url": reverse("charge_qbo_sync", args=[charge.pk]),
        "before_start": False,
    }
    assert qbo_receipts.sync_state(charge, connected=True, start=None)["before_start"] is False


def _answer_after(change, response):
    """A `qbo.request` that lets another writer touch the charge while Intuit answers."""

    def request(*args, **kwargs):
        change()
        return response

    return request


def test_receipt_sender_bump_during_push_still_marks_synced(ready) -> None:
    charge = _card_charge()

    def receipt_sender() -> None:
        now = timezone.now() + timedelta(seconds=1)
        Charge.objects.filter(pk=charge.pk).update(receipt_attempted_at=now, updated_at=now)

    with patch(REQ, side_effect=_answer_after(receipt_sender, _receipt())):
        assert qbo_receipts.sync_pending() == 1
    charge.refresh_from_db()
    assert (charge.qbo_id, charge.qbo_sync_state, charge.qbo_sync_error) == (
        "900",
        SyncState.SYNCED,
        "",
    )


def test_receipt_sender_bump_clears_a_stale_error_on_retry(ready) -> None:
    charge = _card_charge(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Old sentence")

    def receipt_sender() -> None:
        now = timezone.now() + timedelta(seconds=1)
        Charge.objects.filter(pk=charge.pk).update(receipt_sent_at=now, updated_at=now)

    with patch(REQ, side_effect=_answer_after(receipt_sender, _receipt())):
        assert qbo_receipts.push_receipt(charge) is True
    charge.refresh_from_db()
    assert (charge.qbo_sync_state, charge.qbo_sync_error) == (SyncState.SYNCED, "")


def test_real_change_during_push_stays_not_synced(ready) -> None:
    charge = _card_charge()

    def edit() -> None:
        now = timezone.now() + timedelta(seconds=1)
        Charge.objects.filter(pk=charge.pk).update(amount=Decimal("650.00"), updated_at=now)

    with patch(REQ, side_effect=_answer_after(edit, _receipt())):
        assert qbo_receipts.sync_pending() == 0
    charge.refresh_from_db()
    assert (charge.qbo_id, charge.qbo_sync_state) == ("900", SyncState.NOT_SYNCED)
    assert not _alerts().exists()


def test_links_and_receipts_share_one_batch(ready) -> None:
    charges = [_card_charge() for _ in range(3)]
    for charge in charges[:2]:
        CustomerLink.objects.filter(contact=charge.reservation.lead.contact).delete()
    names = [charge.reservation.lead.contact.name for charge in charges[:2]]
    answers = [
        {"Customer": _customer(id_="61", name=names[0])},
        {"Customer": _customer(id_="62", name=names[1])},
        _receipt(),
    ]
    with (
        patch.object(qbo_receipts, "SYNC_BATCH", 3),
        patch(REQ, side_effect=answers) as req,
    ):
        assert qbo_receipts.sync_pending() == 3
    assert _paths(req) == ["customer", "customer", "salesreceipt"]
    # The next run carries on with the receipts left.
    with (
        patch.object(qbo_receipts, "SYNC_BATCH", 3),
        patch(REQ, side_effect=[_receipt(id_="901"), _receipt(id_="902")]) as req,
    ):
        assert qbo_receipts.sync_pending() == 2
    assert _paths(req) == ["salesreceipt", "salesreceipt"]
