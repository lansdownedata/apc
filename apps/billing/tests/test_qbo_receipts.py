"""APC-108 — QuickBooks card customers, Sales Receipts and Refund Receipts."""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import qbo_receipts
from apps.billing.factories import BillingAccountFactory, CustomerLinkFactory
from apps.billing.models import CustomerLink, QboAccountMapping, SyncState
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.integrations import qbo
from apps.notifications.models import Notification
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
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
