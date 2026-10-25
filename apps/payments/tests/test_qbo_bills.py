"""APC-74 — affiliate bills and bill payments pushed to QuickBooks. Push-only: the portal is
the system of record, nothing is read back except the company's chart of accounts (a cache).

The QuickBooks client is mocked at its `request()` boundary, as in APC-42's tests. Nothing
here calls Intuit.
"""

import html
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.billing import services as billing
from apps.billing.models import QboAccount, QboAccountMapping, SyncState
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.integrations import qbo
from apps.notifications.models import Notification
from apps.payments import qbo_bills
from apps.payments.models import AffiliatePayable
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db
REQ = "apps.payments.qbo_bills.qbo.request"
REALM = "9341"


@pytest.fixture(autouse=True)
def connected():
    with (
        patch("apps.payments.qbo_bills.qbo.is_connected", return_value=True),
        patch("apps.billing.qbo_sync.qbo.is_connected", return_value=True),
        patch("apps.billing.qbo_sync.current_realm", return_value=REALM),
    ):
        yield


def _fault(code, message="", status=400, detail=""):
    error = {"code": code, "Message": message}
    if detail:
        error["Detail"] = detail
    return qbo.QboAPIError(status, json.dumps({"Fault": {"Error": [error]}}))


def _accounts(*rows):
    return {"QueryResponse": {"Account": list(rows)}}


def _acct(qbo_id, name, kind, active=True):
    return {
        "Id": qbo_id,
        "Name": name,
        "FullyQualifiedName": name,
        "AccountType": kind,
        "Active": active,
    }


# --- the chart of accounts cache and the mapping ------------------------------------------


def test_refreshing_accounts_caches_expense_and_bank_accounts_and_retires_the_rest():
    QboAccount.objects.create(realm_id=REALM, qbo_id="99", name="Old", account_type="Expense")
    rows = _accounts(
        _acct("7", "Subcontracted transportation", "Cost of Goods Sold"),
        _acct("8", "Checking", "Bank"),
        _acct("9", "Sales", "Income"),
    )

    with patch(REQ, return_value=rows) as req:
        qbo_bills.refresh_accounts()

    assert req.call_args.args == ("GET", "query")
    assert "from Account" in req.call_args.kwargs["params"]["query"]
    live = QboAccount.objects.filter(active=True)
    assert set(live.values_list("qbo_id", flat=True)) == {"7", "8"}  # no income accounts
    assert QboAccount.objects.get(qbo_id="99").active is False


def test_the_mapping_is_only_good_for_active_accounts_in_the_connected_company():
    expense = QboAccount.objects.create(
        realm_id=REALM, qbo_id="7", name="Subcontracted", account_type="Cost of Goods Sold"
    )
    bank = QboAccount.objects.create(
        realm_id="other", qbo_id="8", name="Checking", account_type="Bank"
    )
    config = QboAccountMapping.load()
    config.expense_account = expense
    config.payment_account = bank
    config.save()

    assert qbo_bills.expense_account_ref() == {"value": "7"}
    assert qbo_bills.payment_account_ref() is None  # another company's account

    expense.active = False
    expense.save()
    assert qbo_bills.expense_account_ref() is None


def test_accounts_offered_for_each_choice():
    subs = QboAccount.objects.create(
        realm_id=REALM, qbo_id="7", name="Subs", account_type="Expense"
    )
    bank = QboAccount.objects.create(
        realm_id=REALM, qbo_id="8", name="Checking", account_type="Bank"
    )
    QboAccount.objects.create(realm_id="other", qbo_id="1", name="Theirs", account_type="Bank")

    assert qbo_bills.expense_choices() == [(subs.pk, "Subs")]
    assert qbo_bills.payment_choices() == [(bank.pk, "Checking")]


# --- fixtures for vendors, bills and payments ------------------------------------------------


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _map(expense=True, payment=True, bills_from=date(2026, 10, 1)):
    config = QboAccountMapping.load()
    config.bills_from = bills_from
    if expense:
        config.expense_account = QboAccount.objects.get_or_create(
            realm_id=REALM,
            qbo_id="70",
            defaults={"name": "Subcontracted transportation", "account_type": "Cost of Goods Sold"},
        )[0]
    if payment:
        config.payment_account = QboAccount.objects.get_or_create(
            realm_id=REALM, qbo_id="35", defaults={"name": "Checking", "account_type": "Bank"}
        )[0]
    config.save()
    return config


def _payable(
    *,
    status=AffiliatePayable.Status.APPROVED,
    invoice="470.00",
    payout="420.00",
    number="INV-77",
    vendor=None,
    file=None,
    lead=None,
    **extra,
) -> AffiliatePayable:
    """A $420 farm-out that ran on the evening of Oct 20 ET; the invoice came in at 11:30 PM
    ET that day (already Oct 21 in UTC)."""
    trip = ReservationFactory(
        pickup_timezone="America/New_York", **({"lead": lead} if lead else {})
    )
    vendor = vendor or VendorFactory(
        name="Capital Coach", email="ops@capital.test", phone="+12025550100"
    )
    assignment = AssignmentFactory(
        reservation=trip,
        vendor=vendor,
        status=Assignment.Status.CONFIRMED,
        payout=Decimal(payout),
    )
    received = datetime(2026, 10, 21, 3, 30, tzinfo=UTC)
    return AffiliatePayable.objects.create(
        assignment=assignment,
        expected_amount=Decimal(invoice),
        invoice_amount=None if invoice is None else Decimal(invoice),
        invoice_number=number,
        invoice_file=file or "",
        invoice_received_at=received,
        status=status,
        approved_at=received,
        **extra,
    )


def _paid(**kw) -> AffiliatePayable:
    return _payable(
        status=AffiliatePayable.Status.PAID,
        paid_at=datetime(2026, 10, 24, 15, 0, tzinfo=UTC),
        paid_method=AffiliatePayable.Method.ACH,
        paid_reference="ACH 5521",
        **kw,
    )


def _linked_vendor(**kw) -> Vendor:
    return VendorFactory(
        name="Capital Coach",
        qbo_id="56",
        qbo_sync_token="0",
        qbo_realm_id=REALM,
        qbo_sync_state=SyncState.SYNCED,
        **kw,
    )


def _vendor(qbo_id="56", token="0", name="Capital Coach"):
    return {"Vendor": {"Id": qbo_id, "SyncToken": token, "DisplayName": name}}


def _bill(qbo_id="145", token="0", doc="INV-77", vendor="56", total=470.0):
    return {
        "Bill": {
            "Id": qbo_id,
            "SyncToken": token,
            "DocNumber": doc,
            "VendorRef": {"value": vendor},
            "TotalAmt": total,
        }
    }


def _billpayment(qbo_id="190", token="0", vendor="56", total=470.0):
    return {
        "BillPayment": {
            "Id": qbo_id,
            "SyncToken": token,
            "VendorRef": {"value": vendor},
            "TotalAmt": total,
        }
    }


def _bill_synced(payable, qbo_id="145"):
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_id=qbo_id, qbo_sync_token="0", qbo_realm_id=REALM, qbo_sync_state=SyncState.SYNCED
    )
    payable.refresh_from_db()
    return payable


# --- vendors ------------------------------------------------------------------------------------


def test_a_vendor_is_created_once_with_its_contact_details_then_linked_from_then_on():
    payable = _payable()
    vendor = payable.assignment.vendor

    with patch(REQ, return_value=_vendor()) as req:
        assert qbo_bills.push_vendor(vendor) is True

    method, path = req.call_args.args
    body = req.call_args.kwargs["json"]
    assert (method, path) == ("POST", "vendor")
    assert body["DisplayName"] == "Capital Coach"
    assert body["PrimaryEmailAddr"] == {"Address": "ops@capital.test"}
    assert body["PrimaryPhone"] == {"FreeFormNumber": "+12025550100"}
    assert "Id" not in body
    vendor.refresh_from_db()
    assert (vendor.qbo_id, vendor.qbo_sync_state, vendor.qbo_realm_id) == (
        "56",
        SyncState.SYNCED,
        REALM,
    )

    with patch(REQ) as req:
        qbo_bills.sync_pending()
    assert not [c for c in req.call_args_list if c.args[1] == "vendor"]


def test_a_vendor_with_no_bills_is_never_pushed():
    VendorFactory()

    with patch(REQ) as req:
        assert qbo_bills.sync_pending() == 0

    req.assert_not_called()


def test_a_vendor_already_in_quickbooks_by_that_name_is_linked_not_duplicated():
    vendor = _payable().assignment.vendor
    existing = {
        "QueryResponse": {
            "Vendor": [{"Id": "31", "SyncToken": "4", "DisplayName": "Capital Coach"}]
        }
    }
    calls = [_fault("6240", "Duplicate Name Exists Error"), existing, _vendor("31", "5")]

    with patch(REQ, side_effect=calls) as req:
        assert qbo_bills.push_vendor(vendor) is True

    lookup = req.call_args_list[1]
    assert lookup.args == ("GET", "query")
    assert "from Vendor where DisplayName = 'Capital Coach'" in lookup.kwargs["params"]["query"]
    update = req.call_args_list[2].kwargs["json"]
    assert (update["Id"], update["SyncToken"], update["sparse"]) == ("31", "4", True)
    vendor.refresh_from_db()
    assert (vendor.qbo_id, vendor.qbo_sync_token) == ("31", "5")


def test_a_vendor_name_held_by_a_customer_is_a_readable_error_and_one_alert():
    vendor = _payable().assignment.vendor
    calls = [_fault("6240", "Duplicate Name Exists Error"), {"QueryResponse": {}}]

    with patch(REQ, side_effect=calls):
        assert qbo_bills.push_vendor(vendor) is False

    vendor.refresh_from_db()
    assert vendor.qbo_sync_state == SyncState.ERROR
    assert vendor.qbo_sync_error.startswith("QuickBooks already has a name like “Capital Coach”")
    assert vendor.qbo_sync_error.endswith("Rename the vendor here or there, then retry.")
    alert = Notification.objects.get()
    assert alert.vendor_id == vendor.pk and "Capital Coach" in alert.title


def test_a_name_lookup_escapes_apostrophes():
    vendor = _payable(vendor=VendorFactory(name="O'Hare Limo")).assignment.vendor
    calls = [_fault("6240"), {"QueryResponse": {}}]

    with patch(REQ, side_effect=calls) as req:
        qbo_bills.push_vendor(vendor)

    assert "DisplayName = 'O\\'Hare Limo'" in req.call_args_list[1].kwargs["params"]["query"]


def test_editing_a_linked_vendors_name_queues_an_update(client):
    from django.urls import reverse

    from apps.accounts.factories import UserFactory

    vendor = _linked_vendor()
    client.force_login(UserFactory())
    data = {
        "name": "Capital Coach LLC",
        "status": "active",
        "contact_name": "",
        "phone": vendor.phone,
        "email": vendor.email,
    }
    client.post(reverse("vendor_edit", args=[vendor.pk]), data)

    vendor.refresh_from_db()
    assert vendor.name == "Capital Coach LLC"
    assert vendor.qbo_sync_state == SyncState.NOT_SYNCED

    with patch(REQ, return_value=_vendor("56", "1", "Capital Coach LLC")) as req:
        qbo_bills.push_vendor(vendor)
    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["SyncToken"], body["DisplayName"]) == ("56", "0", "Capital Coach LLC")


# --- bills -------------------------------------------------------------------------------------


def test_approve_pushes_one_bill_with_two_lines_and_the_invoice_number():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, return_value=_bill()) as req:
        assert qbo_bills.push_bill(payable) is True

    method, path = req.call_args.args
    body = req.call_args.kwargs["json"]
    assert (method, path) == ("POST", "bill")
    assert body["VendorRef"] == {"value": "56"}
    assert body["DocNumber"] == "INV-77"
    assert body["TxnDate"] == "2026-10-20"  # the trip's date, not UTC's Oct 21
    base, overtime = body["Line"]
    for line in (base, overtime):
        assert line["DetailType"] == "AccountBasedExpenseLineDetail"
        assert line["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "70"}
    assert (base["Amount"], overtime["Amount"]) == (420.0, 50.0)
    assert "base" in base["Description"].lower() and "overtime" in overtime["Description"].lower()
    payable.refresh_from_db()
    assert (payable.qbo_id, payable.qbo_sync_token, payable.qbo_sync_state) == (
        "145",
        "0",
        SyncState.SYNCED,
    )


def test_a_bill_with_no_overtime_has_one_line():
    _map()
    payable = _payable(invoice="420.00", vendor=_linked_vendor())

    with patch(REQ, return_value=_bill()) as req:
        qbo_bills.push_bill(payable)

    assert [line["Amount"] for line in req.call_args.kwargs["json"]["Line"]] == [420.0]


def test_an_invoice_under_the_payout_bills_the_invoice_amount_never_a_negative_line():
    _map()
    payable = _payable(invoice="400.00", vendor=_linked_vendor(), approval_note="Late pickup")

    with patch(REQ, return_value=_bill()) as req:
        qbo_bills.push_bill(payable)

    assert [line["Amount"] for line in req.call_args.kwargs["json"]["Line"]] == [400.0]


def test_bill_lines_carry_the_customers_group_for_cost_per_job():
    _map()
    contact = ContactFactory()
    account = billing.create_account(contact, name="Ridgeline", group_name="Accounts Payable")
    account.groups.update(qbo_id="59", qbo_realm_id=REALM, qbo_sync_state=SyncState.SYNCED)
    payable = _payable(vendor=_linked_vendor(), lead=ReservationFactory(lead__contact=contact).lead)

    with patch(REQ, return_value=_bill()) as req:
        qbo_bills.push_bill(payable)

    for line in req.call_args.kwargs["json"]["Line"]:
        detail = line["AccountBasedExpenseLineDetail"]
        assert detail["CustomerRef"] == {"value": "59"}
        assert detail["BillableStatus"] == "NotBillable"


def test_a_card_customers_bill_lines_have_no_customer():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, return_value=_bill()) as req:
        qbo_bills.push_bill(payable)

    assert (
        "CustomerRef"
        not in req.call_args.kwargs["json"]["Line"][0]["AccountBasedExpenseLineDetail"]
    )


def test_a_long_invoice_number_is_cut_to_quickbooks_limit_and_kept_whole_in_the_memo():
    _map()
    number = "AFFILIATE-INVOICE-2026-10-000123"
    payable = _payable(number=number, vendor=_linked_vendor())

    with patch(REQ, return_value=_bill(doc=number[:21])) as req:
        assert qbo_bills.push_bill(payable) is True

    body = req.call_args.kwargs["json"]
    assert body["DocNumber"] == number[:21]
    assert number in body["PrivateNote"]


def test_a_re_push_updates_the_same_bill_never_a_second_one():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor()))
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        invoice_amount=Decimal("480.00"), qbo_sync_state=SyncState.NOT_SYNCED
    )
    payable.refresh_from_db()

    with patch(REQ, return_value=_bill(token="1", total=480.0)) as req:
        assert qbo_bills.push_bill(payable) is True

    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["SyncToken"]) == ("145", "0")
    assert sum(line["Amount"] for line in body["Line"]) == 480.0
    payable.refresh_from_db()
    assert payable.qbo_sync_token == "1"


def test_a_retried_bill_create_sends_the_same_request_id():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, side_effect=[qbo.QboAPIError(0, "network: Timeout"), _bill()]) as req:
        with pytest.raises(qbo_bills.SyncPaused):
            qbo_bills.push_bill(payable)
        qbo_bills.push_bill(payable)

    first, second = (c.kwargs["params"]["requestid"] for c in req.call_args_list)
    assert first == second and len(first) <= 50


def test_a_stale_bill_is_re_read_once_and_retried():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor()))
    AffiliatePayable.objects.filter(pk=payable.pk).update(qbo_sync_state=SyncState.NOT_SYNCED)
    payable.refresh_from_db()
    calls = [
        _fault("5010", "Stale Object Error"),
        {"Bill": {"Id": "145", "SyncToken": "3"}},
        _bill(token="4"),
    ]

    with patch(REQ, side_effect=calls) as req:
        assert qbo_bills.push_bill(payable) is True

    assert req.call_args_list[1].args == ("GET", "bill/145")
    assert req.call_args_list[2].kwargs["json"]["SyncToken"] == "3"


def test_a_bill_waits_for_its_vendor_and_never_errors_on_its_own():
    _map()
    payable = _payable()

    with patch(REQ) as req:
        assert qbo_bills.push_bill(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert (payable.qbo_sync_state, payable.qbo_sync_error) == (SyncState.NOT_SYNCED, "")


def test_a_bill_is_refused_with_a_clear_error_until_the_expense_account_is_chosen():
    _map(expense=False)
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ) as req:
        assert qbo_bills.push_bill(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert payable.qbo_sync_state == SyncState.ERROR
    assert "expense account" in payable.qbo_sync_error
    assert "Settings" in payable.qbo_sync_error


def test_a_missing_mapping_alerts_once_for_the_whole_run_not_per_bill():
    _map(expense=False)
    vendor = _linked_vendor()
    for n in range(3):
        _payable(vendor=vendor, number=f"INV-{n}")

    with patch(REQ):
        qbo_bills.sync_pending()
        qbo_bills.sync_pending()

    assert AffiliatePayable.objects.filter(qbo_sync_state=SyncState.ERROR).count() == 3
    assert Notification.objects.count() == 1


def test_a_refused_bill_shows_intuits_reason_and_alerts_on_the_order():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(
        REQ,
        side_effect=_fault(
            "6000", "A business validation error has occurred", detail="Account is inactive"
        ),
    ):
        assert qbo_bills.push_bill(payable) is False

    payable.refresh_from_db()
    assert payable.qbo_sync_state == SyncState.ERROR
    assert "Account is inactive" in payable.qbo_sync_error
    alert = Notification.objects.get()
    assert alert.lead_id == payable.assignment.reservation.lead_id


def test_an_outage_pauses_the_bill_push_and_marks_nothing():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, side_effect=qbo.QboAPIError(503, "down")):
        with pytest.raises(qbo_bills.SyncPaused):
            qbo_bills.push_bill(payable)

    payable.refresh_from_db()
    assert (payable.qbo_sync_state, payable.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert not Notification.objects.exists()


def test_draft_and_zero_dollar_payables_are_not_bills():
    _map()
    vendor = _linked_vendor()
    _payable(status=AffiliatePayable.Status.DRAFT, vendor=vendor)
    _payable(invoice="0.00", vendor=vendor)

    with patch(REQ) as req:
        assert qbo_bills.sync_pending() == 0

    req.assert_not_called()


def test_a_bill_edited_while_in_flight_is_not_marked_synced():
    _map()
    payable = _payable(vendor=_linked_vendor())

    def edited_meanwhile(*args, **kwargs):
        AffiliatePayable.objects.get(pk=payable.pk).save()  # bumps updated_at
        return _bill()

    with patch(REQ, side_effect=edited_meanwhile):
        qbo_bills.push_bill(payable)

    payable.refresh_from_db()
    assert payable.qbo_id == "145"
    assert payable.qbo_sync_state == SyncState.NOT_SYNCED


# --- the invoice file ----------------------------------------------------------------------------


def _pdf():
    return SimpleUploadedFile("inv-77.pdf", b"%PDF-1.4 invoice", content_type="application/pdf")


def _attached(attachable_id="900"):
    return {"AttachableResponse": [{"Attachable": {"Id": attachable_id}}]}


def test_the_invoice_file_is_attached_to_the_bill_once():
    _map()
    payable = _payable(vendor=_linked_vendor(), file=_pdf())

    with patch(REQ, side_effect=[_bill(), _attached()]) as req:
        assert qbo_bills.sync_pending() == 2

    upload = req.call_args_list[1]
    assert upload.args == ("POST", "upload")
    files = upload.kwargs["files"]
    meta = json.loads(files["file_metadata_01"][1])
    assert meta["AttachableRef"] == [{"EntityRef": {"type": "Bill", "value": "145"}}]
    assert meta["ContentType"] == "application/pdf"
    name, content, ctype = files["file_content_01"]
    assert name.endswith(".pdf") and content == b"%PDF-1.4 invoice" and ctype == "application/pdf"
    payable.refresh_from_db()
    assert (payable.qbo_attachment_id, payable.qbo_attachment_sync_state) == (
        "900",
        SyncState.SYNCED,
    )

    AffiliatePayable.objects.filter(pk=payable.pk).update(qbo_sync_state=SyncState.NOT_SYNCED)
    with patch(REQ, return_value=_bill(token="1")) as req:
        qbo_bills.sync_pending()
    assert [c.args[1] for c in req.call_args_list] == ["bill"]  # not attached twice


def _refused_file():
    return {
        "AttachableResponse": [
            {"Fault": {"Error": [{"code": "6000", "Detail": "File type is not supported"}]}}
        ]
    }


def test_a_refused_file_has_its_own_error_and_never_holds_up_the_bill_or_payment():
    _map()
    payable = _paid(vendor=_linked_vendor(), file=_pdf())

    with patch(REQ, side_effect=[_bill(), _billpayment(), _refused_file()]) as req:
        qbo_bills.sync_pending()

    assert [c.args[1] for c in req.call_args_list] == ["bill", "billpayment", "upload"]
    payable.refresh_from_db()
    assert (payable.qbo_sync_state, payable.qbo_payment_sync_state) == (
        SyncState.SYNCED,
        SyncState.SYNCED,
    )
    assert payable.qbo_attachment_sync_state == SyncState.ERROR
    assert payable.qbo_attachment_sync_error == (
        "QuickBooks didn't accept the invoice file: File type is not supported"
    )
    state = qbo_bills.sync_state(payable)
    assert state["attachment_state"] == "error"
    assert "File type" in state["attachment_error"]


def test_a_file_quickbooks_refused_is_retried_under_a_new_request_id():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor(), file=_pdf()))

    with patch(REQ, side_effect=[_refused_file(), _attached()]) as req:
        assert qbo_bills.push_attachment(payable) is False
        assert qbo_bills.push_attachment(payable) is True

    first, second = (c.kwargs["params"]["requestid"] for c in req.call_args_list)
    assert first != second and len(second) <= 50


def test_an_upload_error_status_is_worded_as_the_file_not_the_bill():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor(), file=_pdf()))

    with patch(REQ, side_effect=_fault("6000", detail="Unsupported media")):
        qbo_bills.push_attachment(payable)

    payable.refresh_from_db()
    assert payable.qbo_attachment_sync_error == (
        "QuickBooks didn't accept the invoice file: Unsupported media"
    )
    assert payable.qbo_sync_state == SyncState.SYNCED


def test_a_file_over_20_mb_is_refused_here_without_a_call():
    _map()
    big = SimpleUploadedFile("big.pdf", b"0" * (20 * 1024 * 1024 + 1), "application/pdf")
    payable = _bill_synced(_payable(vendor=_linked_vendor(), file=big))

    with patch(REQ) as req:
        assert qbo_bills.push_attachment(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert "20 MB" in payable.qbo_attachment_sync_error


def test_the_file_waits_for_its_bill():
    _map()
    payable = _payable(vendor=_linked_vendor(), file=_pdf())

    with patch(REQ) as req:
        assert qbo_bills.push_attachment(payable) is False

    req.assert_not_called()


# --- bill payments ---------------------------------------------------------------------------


def test_mark_paid_pushes_one_bill_payment_linked_to_that_bill():
    _map()
    payable = _bill_synced(_paid(vendor=_linked_vendor()))

    with patch(REQ, return_value=_billpayment()) as req:
        assert qbo_bills.push_payment(payable) is True

    method, path = req.call_args.args
    body = req.call_args.kwargs["json"]
    assert (method, path) == ("POST", "billpayment")
    assert body["VendorRef"] == {"value": "56"}
    assert body["PayType"] == "Check"
    assert body["CheckPayment"] == {"BankAccountRef": {"value": "35"}}
    assert body["TotalAmt"] == 470.0
    assert body["TxnDate"] == "2026-10-24"
    assert body["Line"] == [{"Amount": 470.0, "LinkedTxn": [{"TxnId": "145", "TxnType": "Bill"}]}]
    assert "ACH" in body["PrivateNote"] and "ACH 5521" in body["PrivateNote"]
    payable.refresh_from_db()
    assert (payable.qbo_payment_id, payable.qbo_payment_sync_state) == ("190", SyncState.SYNCED)
    assert payable.qbo_sync_state == SyncState.SYNCED  # the bill's own state is untouched


def test_a_payment_waits_for_its_bill():
    _map()
    payable = _paid(vendor=_linked_vendor())

    with patch(REQ) as req:
        assert qbo_bills.push_payment(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert payable.qbo_payment_sync_state == SyncState.NOT_SYNCED


def test_a_payment_is_refused_until_the_bank_account_is_chosen():
    _map(payment=False)
    payable = _bill_synced(_paid(vendor=_linked_vendor()))

    with patch(REQ) as req:
        assert qbo_bills.push_payment(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert payable.qbo_payment_sync_state == SyncState.ERROR
    assert "bank account" in payable.qbo_payment_sync_error


def test_a_payment_failure_is_recorded_and_the_next_run_retries_it():
    _map()
    payable = _bill_synced(_paid(vendor=_linked_vendor()))

    with patch(REQ, side_effect=_fault("6000", detail="Amount exceeds the bill balance")):
        qbo_bills.sync_pending()
    payable.refresh_from_db()
    assert payable.qbo_payment_sync_state == SyncState.ERROR
    assert "exceeds the bill balance" in payable.qbo_payment_sync_error

    with patch(REQ, return_value=_billpayment()) as req:
        assert qbo_bills.sync_pending() == 1
    assert req.call_args.args == ("POST", "billpayment")
    payable.refresh_from_db()
    assert (payable.qbo_payment_sync_state, payable.qbo_payment_sync_error) == (
        SyncState.SYNCED,
        "",
    )


# --- the run ----------------------------------------------------------------------------------


def test_a_run_pushes_the_vendor_then_the_bill_then_the_payment():
    _map()
    _paid()

    with patch(REQ, side_effect=[_vendor(), _bill(), _billpayment()]) as req:
        assert qbo_bills.sync_pending() == 3

    assert [c.args[1] for c in req.call_args_list] == ["vendor", "bill", "billpayment"]
    assert qbo_bills.sync_pending() == 0


def test_an_outage_mid_run_stops_the_run_and_marks_nothing():
    _map()
    for n in range(3):
        _payable(vendor=_linked_vendor(), number=f"INV-{n}")

    with patch(REQ, side_effect=qbo.QboRateLimited("throttled", 30)) as req:
        assert qbo_bills.sync_pending() == 0

    assert req.call_count == 1
    assert not AffiliatePayable.objects.exclude(qbo_sync_state=SyncState.NOT_SYNCED).exists()


def test_nothing_is_sent_while_quickbooks_is_not_connected():
    _map()
    _payable(vendor=_linked_vendor())

    with patch("apps.payments.qbo_bills.qbo.is_connected", return_value=False), patch(REQ) as req:
        assert qbo_bills.sync_pending() == 0

    req.assert_not_called()


def test_bills_from_another_quickbooks_company_are_pushed_again_as_new():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor()))
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_realm_id="111", qbo_attachment_id="900", qbo_attachment_realm_id="111"
    )
    Vendor.objects.filter(pk=payable.assignment.vendor_id).update(qbo_realm_id="111")

    with patch(REQ, side_effect=[_vendor("8"), _bill("9", vendor="8")]) as req:
        assert qbo_bills.sync_pending() == 2

    assert "Id" not in req.call_args_list[0].kwargs["json"]
    assert "Id" not in req.call_args_list[1].kwargs["json"]
    payable.refresh_from_db()
    assert (payable.qbo_id, payable.qbo_realm_id, payable.qbo_attachment_id) == ("9", REALM, "")


# --- what queues a push ---------------------------------------------------------------------------


def test_approving_queues_the_bill_and_marking_paid_queues_the_payment():
    from apps.accounts.factories import UserFactory
    from apps.payments import payables

    user = UserFactory(can_manage_payments=True)
    payable = _payable(status=AffiliatePayable.Status.DRAFT, invoice="420.00", payout="420.00")
    assert not qbo_bills.pending_bills(REALM).filter(pk=payable.pk).exists()

    with patch("apps.tasks.services.sync"):
        payables.approve(payable, user=user)
        assert qbo_bills.pending_bills(REALM).filter(pk=payable.pk).exists()
        _bill_synced(payable)
        payables.mark_paid(
            payable, user=user, method=AffiliatePayable.Method.CHECK, reference="1043"
        )

    assert qbo_bills.pending_payments(REALM).filter(pk=payable.pk).exists()
    assert not qbo_bills.pending_bills(REALM).filter(pk=payable.pk).exists()


def _admin_correct(admin_client, payable, amount):
    from django.urls import reverse

    return admin_client.post(
        reverse("admin:payments_affiliatepayable_change", args=[payable.pk]),
        {"invoice_number": "INV-77", "invoice_amount": amount, "approval_note": "Toll added"},
    )


LOCKED = "Approved payables can't change amount here; corrections come with Trip Processing."


@pytest.mark.parametrize("status", [AffiliatePayable.Status.APPROVED, AffiliatePayable.Status.PAID])
def test_an_approved_or_paid_payables_amount_cant_change_in_the_admin(admin_client, status):
    """The ledger accrued (and paid) the approved amount, and QuickBooks has it as a bill:
    an admin edit would drift both away from the portal (Moe's default, 2026-10-25)."""
    payable = _bill_synced(_payable(status=status, vendor=_linked_vendor()))

    resp = _admin_correct(admin_client, payable, "480.00")

    assert LOCKED in html.unescape(resp.content.decode())
    payable.refresh_from_db()
    assert payable.invoice_amount == Decimal("470.00")
    assert payable.qbo_sync_state == SyncState.SYNCED


def test_an_approved_payables_invoice_number_cant_change_in_the_admin_either(admin_client):
    from django.urls import reverse

    payable = _payable(vendor=_linked_vendor())

    resp = admin_client.post(
        reverse("admin:payments_affiliatepayable_change", args=[payable.pk]),
        {"invoice_number": "INV-78", "invoice_amount": "470.00"},
    )

    assert LOCKED in html.unescape(resp.content.decode())
    payable.refresh_from_db()
    assert payable.invoice_number == "INV-77"


def test_a_draft_payable_can_still_be_corrected_in_the_admin(admin_client):
    payable = _payable(status=AffiliatePayable.Status.DRAFT)

    _admin_correct(admin_client, payable, "480.00")

    payable.refresh_from_db()
    assert payable.invoice_amount == Decimal("480.00")


def test_one_bill_is_one_approved_payable_today():
    """Which payables make up a QuickBooks Bill lives in one place (`bill_members`): one trip
    per bill now; one vendor invoice covering several trips is the likely next step."""
    payable = _payable()

    assert qbo_bills.bill_members(payable) == [payable]


def test_renaming_a_vendor_in_the_admin_queues_it(admin_client):
    from django.urls import reverse

    vendor = _linked_vendor()
    data = {
        "name": "Capital Coach LLC",
        "status": "active",
        "phone": vendor.phone,
        "email": vendor.email,
        "contact_name": "",
        "service_area": "",
        "qbo_sync_state": SyncState.SYNCED,
    }
    for prefix in ("drivers", "documents", "policies"):
        data |= {f"{prefix}-TOTAL_FORMS": "0", f"{prefix}-INITIAL_FORMS": "0"}
    admin_client.post(reverse("admin:vendors_vendor_change", args=[vendor.pk]), data)

    vendor.refresh_from_db()
    assert vendor.name == "Capital Coach LLC"
    assert vendor.qbo_sync_state == SyncState.NOT_SYNCED


# --- the hourly job and Retry -----------------------------------------------------------------

from django.urls import reverse  # noqa: E402

from apps.accounts.factories import UserFactory  # noqa: E402
from apps.core.cron import JOBS  # noqa: E402


def test_the_hourly_job_does_customers_then_refreshes_accounts_then_pushes_bills():
    from apps.billing.models import QboTerm

    QboTerm.objects.create(qbo_id="3", name="Net 30", active=True)
    billing.create_account(ContactFactory(), name="Ridgeline", group_name="AP")
    _map()
    _paid()
    terms = {"QueryResponse": {"Term": [{"Id": "3", "Name": "Net 30", "Active": True}]}}
    accounts = _accounts(
        _acct("70", "Subcontracted transportation", "Cost of Goods Sold"),
        _acct("35", "Checking", "Bank"),
    )
    customer = {"Customer": {"Id": "58", "SyncToken": "0"}}
    group = {"Customer": {"Id": "59", "SyncToken": "0"}}
    calls = [terms, customer, group, accounts, _vendor(), _bill(), _billpayment()]

    with patch(REQ, side_effect=calls) as req:
        assert JOBS["retry-qbo-sync"]() == 5

    paths = [c.args[1] for c in req.call_args_list]
    assert paths == ["query", "customer", "customer", "query", "vendor", "bill", "billpayment"]
    assert "from Account" in req.call_args_list[3].kwargs["params"]["query"]


def test_a_failed_accounts_refresh_still_pushes_with_the_cached_choice():
    _map()
    _payable(vendor=_linked_vendor())
    terms = {"QueryResponse": {"Term": []}}

    with patch(REQ, side_effect=[terms, qbo.QboAPIError(503, "down"), _bill()]):
        assert JOBS["retry-qbo-sync"]() == 1


def test_the_hourly_job_does_nothing_without_a_connection():
    _map()
    _payable(vendor=_linked_vendor())

    with (
        patch("apps.payments.qbo_bills.qbo.is_connected", return_value=False),
        patch("apps.billing.qbo_sync.qbo.is_connected", return_value=False),
        patch(REQ) as req,
    ):
        assert JOBS["retry-qbo-sync"]() == 0

    req.assert_not_called()


@pytest.fixture
def cashier(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def test_retry_on_a_payable_pushes_vendor_bill_and_payment_and_answers_with_the_payable(cashier):
    _map()
    payable = _paid()
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error="QuickBooks didn't accept this bill: x"
    )

    with patch(REQ, side_effect=[_vendor(), _bill(), _billpayment()]):
        resp = cashier.post(reverse("payable_sync", args=[payable.pk]))

    body = resp.json()
    assert body["ok"] is True
    assert body["payable"]["qbo"] == {
        "bill_state": "synced",
        "bill_error": "",
        "payment_state": "synced",
        "payment_error": "",
        "attachment_state": "",  # no invoice file on this one
        "attachment_error": "",
        "sync_url": reverse("payable_sync", args=[payable.pk]),
    }


def test_retry_on_a_payable_reports_a_refusal_in_words(cashier):
    _map(expense=False)
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ):
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    assert body["payable"]["qbo"]["bill_state"] == "error"
    assert "expense account" in body["payable"]["qbo"]["bill_error"]


def test_retry_during_an_outage_says_to_try_again(cashier):
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, side_effect=qbo.QboAPIError(0, "network: Timeout")):
        resp = cashier.post(reverse("payable_sync", args=[payable.pk]))

    assert resp.status_code == 502
    assert "try again" in resp.json()["error"].lower()


def test_retry_needs_payments_access_and_a_connection(client):
    payable = _payable()
    client.force_login(UserFactory())
    assert client.post(reverse("payable_sync", args=[payable.pk])).status_code == 403
    assert (
        client.post(reverse("vendor_sync", args=[payable.assignment.vendor_id])).status_code == 403
    )

    client.force_login(UserFactory(can_manage_payments=True))
    with patch("apps.payments.qbo_bills.qbo.is_connected", return_value=False):
        assert client.post(reverse("payable_sync", args=[payable.pk])).status_code == 400


def test_retry_on_a_vendor(cashier):
    vendor = _payable().assignment.vendor
    Vendor.objects.filter(pk=vendor.pk).update(qbo_sync_state=SyncState.ERROR)

    with patch(REQ, return_value=_vendor()):
        resp = cashier.post(reverse("vendor_sync", args=[vendor.pk]))

    assert resp.json() == {"ok": True, "state": "synced", "error": ""}


def test_a_payable_without_quickbooks_reports_no_sync_state():
    payable = _payable()

    with patch("apps.payments.qbo_bills.qbo.is_connected", return_value=False):
        from apps.payments import payables

        assert payables.payable_json(payable)["qbo"] is None


# --- where staff see it -----------------------------------------------------------------------


def test_the_trip_review_pane_shows_the_quickbooks_state_and_retry(cashier):
    payable = _payable(vendor=_linked_vendor())
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error="QuickBooks didn't accept this bill: x"
    )

    from apps.tasks.models import Task

    trip = payable.assignment.reservation
    Task.objects.create(
        lead=trip.lead, reservation=trip, kind="ops_review", opens_at=trip.created_at
    )  # the order is in review

    html = cashier.get(
        reverse("trip_review_trip", args=[payable.assignment.reservation_id])
    ).content.decode()

    assert "retryPayableSync()" in html
    assert "payable.qbo.bill_error" in html
    assert reverse("payable_sync", args=[payable.pk]) in html  # via payable_state's sync_url


def test_the_vendor_page_shows_its_sync_error_and_retry_only_when_connected(cashier):
    vendor = _payable().assignment.vendor
    Vendor.objects.filter(pk=vendor.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error="QuickBooks already has a customer named x"
    )
    url = reverse("vendor_detail", args=[vendor.pk])

    html = cashier.get(url).content.decode()
    assert "QuickBooks already has a customer named x" in html
    assert reverse("vendor_sync", args=[vendor.pk]) in html

    with patch("apps.payments.qbo_bills.qbo.is_connected", return_value=False):
        html = cashier.get(url).content.decode()
    assert reverse("vendor_sync", args=[vendor.pk]) not in html


def test_the_vendor_pages_payables_flag_a_bill_that_isnt_in_quickbooks(cashier):
    payable = _payable()
    AffiliatePayable.objects.filter(pk=payable.pk).update(qbo_sync_state=SyncState.ERROR)

    html = cashier.get(reverse("vendor_detail", args=[payable.assignment.vendor_id])).content
    assert "Not synced" in html.decode()


def test_a_draft_payable_is_never_pushed_even_on_retry():
    _map()
    payable = _payable(status=AffiliatePayable.Status.DRAFT, vendor=_linked_vendor())

    with patch(REQ) as req:
        assert qbo_bills.push_bill(payable) is False
        qbo_bills.push_everything_for(payable)

    req.assert_not_called()


def test_a_replaced_invoice_file_is_uploaded_under_a_new_request_id():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor(), file=_pdf()))
    with patch(REQ, return_value=_attached()) as req:
        qbo_bills.push_attachment(payable)
    first = req.call_args.kwargs["params"]["requestid"]
    payable.refresh_from_db()
    payable.invoice_file = SimpleUploadedFile("inv-77-corrected.pdf", b"%PDF-1.4 v2")
    payable.qbo_attachment_id = ""
    payable.save()

    with patch(REQ, return_value=_attached("901")) as req:
        qbo_bills.push_attachment(payable)

    assert req.call_args.kwargs["params"]["requestid"] != first


def test_the_accounts_cache_and_the_choice_are_in_the_admin(admin_client):
    QboAccountMapping.load()
    for name in ("qboaccount", "qboaccountmapping"):
        assert admin_client.get(reverse(f"admin:billing_{name}_changelist")).status_code == 200


# --- review fixes (2026-10-25) ------------------------------------------------------------------


def test_a_lost_payment_create_then_an_unset_bank_account_keeps_the_request_id():
    """Lost response, then the bank choice unset for a run (refused here, nothing sent), then
    chosen again: the retry must replay the first create, not make a second payment."""
    _map()
    _bill_synced(_paid(vendor=_linked_vendor()))
    with patch(REQ, side_effect=qbo.QboAPIError(0, "network: Timeout")) as req:
        qbo_bills.sync_pending()
    first = req.call_args.kwargs["params"]["requestid"]
    config = QboAccountMapping.load()
    keep, config.payment_account = config.payment_account, None
    config.save()
    with patch(REQ) as req:
        qbo_bills.sync_pending()
    req.assert_not_called()
    config.payment_account = keep
    config.save()

    with patch(REQ, return_value=_billpayment()) as req:
        qbo_bills.sync_pending()

    assert req.call_args.kwargs["params"]["requestid"] == first


def test_a_lost_bill_create_then_an_unset_expense_account_keeps_the_request_id():
    _map()
    _payable(vendor=_linked_vendor())
    with patch(REQ, side_effect=qbo.QboAPIError(0, "network: Timeout")) as req:
        qbo_bills.sync_pending()
    first = req.call_args.kwargs["params"]["requestid"]
    config = QboAccountMapping.load()
    keep, config.expense_account = config.expense_account, None
    config.save()
    with patch(REQ):
        qbo_bills.sync_pending()
    config.expense_account = keep
    config.save()

    with patch(REQ, return_value=_bill()) as req:
        qbo_bills.sync_pending()

    assert req.call_args.kwargs["params"]["requestid"] == first


def test_quickbooks_refusing_a_bill_create_moves_it_to_a_new_request_id():
    _map()
    payable = _payable(vendor=_linked_vendor())
    with patch(REQ, side_effect=_fault("6000", detail="x")) as req:
        qbo_bills.push_bill(payable)
        qbo_bills.push_bill(payable)
    first, second = (c.kwargs["params"]["requestid"] for c in req.call_args_list)

    assert first != second and len(second) <= 50


def test_linking_an_existing_vendor_never_blanks_what_the_bookkeeper_entered():
    vendor = VendorFactory(name="Capital Coach", email="", phone="")
    _payable(vendor=vendor)
    existing = {
        "QueryResponse": {
            "Vendor": [{"Id": "31", "SyncToken": "4", "DisplayName": "Capital Coach"}]
        }
    }

    with patch(REQ, side_effect=[_fault("6240"), existing, _vendor("31", "5")]) as req:
        assert qbo_bills.push_vendor(vendor) is True

    update = req.call_args_list[2].kwargs["json"]
    assert update["DisplayName"] == "Capital Coach"
    for kept in ("PrimaryEmailAddr", "PrimaryPhone", "CompanyName"):
        assert kept not in update


def test_a_name_quickbooks_has_but_not_as_this_vendor_is_worded_carefully():
    vendor = _payable().assignment.vendor

    with patch(REQ, side_effect=[_fault("6240"), {"QueryResponse": {}}]):
        qbo_bills.push_vendor(vendor)

    vendor.refresh_from_db()
    assert vendor.qbo_sync_error.startswith(
        "QuickBooks already has a name like “Capital Coach” (it may differ in case or spacing)"
    )


def test_a_bill_quickbooks_answers_with_a_different_total_stays_queued():
    """A replayed create can answer with an older bill: what came back must be what we sent,
    to the cent, before it reads as in QuickBooks."""
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, return_value=_bill(total=420.0)):
        assert qbo_bills.push_bill(payable) is False

    payable.refresh_from_db()
    assert payable.qbo_id == "145"
    assert payable.qbo_sync_state == SyncState.NOT_SYNCED


def test_a_payment_quickbooks_answers_with_a_different_total_stays_queued():
    _map()
    payable = _bill_synced(_paid(vendor=_linked_vendor()))

    with patch(REQ, return_value=_billpayment(total=1.0)):
        assert qbo_bills.push_payment(payable) is False

    payable.refresh_from_db()
    assert payable.qbo_payment_sync_state == SyncState.NOT_SYNCED


def test_cents_add_up_on_an_awkward_split():
    _map()
    payable = _payable(vendor=_linked_vendor(), invoice="100.10", payout="33.33")

    with patch(REQ, return_value=_bill(total=100.1)):
        assert qbo_bills.push_bill(payable) is True


# --- one push at a time (the lease) -----------------------------------------------------------


def _reentrant(push, record, response):
    """QuickBooks 'answers' only after a second caller (the cron, a double-click) has tried
    the same record. Returns the requestids that were actually POSTed."""
    posts = []

    def fake(method, path, **kw):
        posts.append(kw.get("params", {}).get("requestid"))
        if len(posts) == 1:
            assert push(record) is False  # the second caller skips: already being sent
        return response

    with patch(REQ, side_effect=fake):
        push(record)
    return posts


def test_a_payment_already_being_sent_is_skipped_without_a_call():
    _map()
    payable = _bill_synced(_paid(vendor=_linked_vendor()))
    stale_copy = qbo_bills._payable(payable)

    assert (
        len(_reentrant(lambda p: qbo_bills.push_payment(stale_copy), payable, _billpayment())) == 1
    )
    payable.refresh_from_db()
    assert payable.qbo_payment_push_started_at is None  # released
    assert payable.qbo_payment_sync_state == SyncState.SYNCED


def test_a_bill_already_being_sent_is_skipped_without_a_call():
    _map()
    payable = _payable(vendor=_linked_vendor())

    assert len(_reentrant(qbo_bills.push_bill, payable, _bill())) == 1


def test_a_vendor_already_being_sent_is_skipped_without_a_call():
    vendor = _payable().assignment.vendor

    assert len(_reentrant(qbo_bills.push_vendor, vendor, _vendor())) == 1


def test_a_file_already_being_sent_is_skipped_without_a_call():
    _map()
    payable = _bill_synced(_payable(vendor=_linked_vendor(), file=_pdf()))

    assert len(_reentrant(qbo_bills.push_attachment, payable, _attached())) == 1


def test_a_lease_left_by_a_crashed_push_is_taken_over_after_ten_minutes():
    from datetime import timedelta

    from django.utils import timezone

    _map()
    payable = _payable(vendor=_linked_vendor())
    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_push_started_at=timezone.now() - timedelta(minutes=9)
    )
    with patch(REQ) as req:
        assert qbo_bills.push_bill(payable) is False
    req.assert_not_called()

    AffiliatePayable.objects.filter(pk=payable.pk).update(
        qbo_push_started_at=timezone.now() - timedelta(minutes=11)
    )
    with patch(REQ, return_value=_bill()):
        assert qbo_bills.push_bill(payable) is True


def test_the_lease_is_released_when_quickbooks_is_down():
    _map()
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ, side_effect=qbo.QboAPIError(503, "down")):
        with pytest.raises(qbo_bills.SyncPaused):
            qbo_bills.push_bill(payable)

    payable.refresh_from_db()
    assert payable.qbo_push_started_at is None


# --- which bills go: the "push affiliate bills from" date ---------------------------------------


def test_with_no_start_date_no_bill_or_payment_goes_and_the_run_alerts_once():
    _map(bills_from=None)
    vendor = _linked_vendor()
    for n in range(2):
        _payable(vendor=vendor, number=f"INV-{n}")
    _bill_synced(_paid(vendor=vendor, number="INV-9"))

    with patch(REQ) as req:
        qbo_bills.sync_pending()
        qbo_bills.sync_pending()

    req.assert_not_called()
    waiting = AffiliatePayable.objects.filter(qbo_sync_state=SyncState.ERROR)
    assert waiting.count() == 2
    assert waiting.first().qbo_sync_error == (
        "Waiting: choose the start date for affiliate bills in Settings → QuickBooks."
    )
    assert AffiliatePayable.objects.get(invoice_number="INV-9").qbo_payment_sync_error.startswith(
        "Waiting: choose the start date"
    )
    assert Notification.objects.count() == 1


def test_with_no_start_date_a_vendor_with_only_bills_is_not_pushed():
    _map(bills_from=None)
    _payable()

    with patch(REQ) as req:
        qbo_bills.sync_pending()

    req.assert_not_called()


def test_a_bill_approved_on_the_start_date_in_the_trips_timezone_goes():
    """Approved 11:30 PM ET on Oct 20 — already Oct 21 in UTC. It belongs to Oct 20."""
    _map(bills_from=date(2026, 10, 20))
    _payable(vendor=_linked_vendor())

    with patch(REQ, return_value=_bill()) as req:
        assert qbo_bills.sync_pending() == 1

    assert req.call_args.args == ("POST", "bill")


def test_a_bill_approved_before_the_start_date_never_goes_and_isnt_marked():
    _map(bills_from=date(2026, 10, 21))  # UTC's date of the approval, not the trip's
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ) as req:
        assert qbo_bills.sync_pending() == 0
        assert qbo_bills.push_bill(payable) is False

    req.assert_not_called()
    payable.refresh_from_db()
    assert (payable.qbo_sync_state, payable.qbo_sync_error) == (SyncState.NOT_SYNCED, "")


# --- Retry says why ---------------------------------------------------------------------------


def test_retry_when_the_vendor_is_refused_says_so_instead_of_sent(cashier):
    _map()
    payable = _paid()

    with patch(REQ, side_effect=[_fault("6240"), {"QueryResponse": {}}]):
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    assert body["ok"] is True
    assert body["reason"].startswith("The vendor isn't in QuickBooks: QuickBooks already has")
    assert body["payable"]["qbo"]["bill_state"] == "not_synced"


def test_retry_that_gets_everything_there_has_no_reason(cashier):
    _map()
    payable = _paid()

    with patch(REQ, side_effect=[_vendor(), _bill(), _billpayment()]):
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    assert body["reason"] == ""


def test_retry_on_a_bill_from_before_the_start_date_says_it_isnt_sent(cashier):
    _map(bills_from=date(2026, 10, 25))
    payable = _payable(vendor=_linked_vendor())

    with patch(REQ) as req:
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    req.assert_not_called()
    assert "before Oct 25, 2026" in body["reason"]


def test_retry_while_the_cron_is_sending_it_says_so(cashier):
    from django.utils import timezone

    _map()
    payable = _payable(vendor=_linked_vendor())
    AffiliatePayable.objects.filter(pk=payable.pk).update(qbo_push_started_at=timezone.now())

    with patch(REQ) as req:
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    req.assert_not_called()
    assert "being sent to QuickBooks right now" in body["reason"]


def test_a_refused_file_is_the_reason_even_though_the_bill_and_payment_went(cashier):
    _map()
    payable = _paid(vendor=_linked_vendor(), file=_pdf())

    with patch(REQ, side_effect=[_bill(), _billpayment(), _refused_file()]):
        body = cashier.post(reverse("payable_sync", args=[payable.pk])).json()

    assert body["reason"] == "QuickBooks didn't accept the invoice file: File type is not supported"


def test_the_pane_reads_the_connection_once_for_many_payables():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.payments import payables

    rows = [_payable() for _ in range(3)]
    with patch("apps.payments.qbo_bills.qbo.is_connected", return_value=True) as connected:
        with CaptureQueriesContext(connection):
            states = [payables.payable_json(p, qbo_connected=True) for p in rows]

    connected.assert_not_called()
    assert all(s["qbo"] for s in states)


def test_refreshing_accounts_asks_only_for_the_usable_types_and_reads_every_page():
    """A big chart of accounts must not be cut at one page and the rest retired."""
    pages = [
        _accounts(_acct("1", "A", "Expense"), _acct("2", "B", "Bank")),
        _accounts(_acct("3", "C", "Other Expense")),
    ]

    with patch.object(qbo_bills, "ACCOUNT_PAGE", 2), patch(REQ, side_effect=pages) as req:
        assert qbo_bills.refresh_accounts() == 3

    first, second = (c.kwargs["params"]["query"] for c in req.call_args_list)
    assert "AccountType in ('Expense', 'Cost of Goods Sold', 'Other Expense', 'Bank')" in first
    assert "startposition 1 maxresults 2" in first.lower()
    assert "startposition 3 maxresults 2" in second.lower()


def test_a_failed_page_retires_nothing():
    QboAccount.objects.create(realm_id=REALM, qbo_id="9", name="Kept", account_type="Bank")
    pages = [
        _accounts(_acct("1", "A", "Expense"), _acct("2", "B", "Bank")),
        qbo.QboAPIError(503, "down"),
    ]

    with patch.object(qbo_bills, "ACCOUNT_PAGE", 2), patch(REQ, side_effect=pages):
        with pytest.raises(qbo.QboAPIError):
            qbo_bills.refresh_accounts()

    assert QboAccount.objects.get(qbo_id="9").active is True
