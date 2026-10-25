"""APC-74 — affiliate bills and bill payments pushed to QuickBooks. Push-only: the portal is
the system of record, nothing is read back except the company's chart of accounts (a cache).

The QuickBooks client is mocked at its `request()` boundary, as in APC-42's tests. Nothing
here calls Intuit.
"""

import json
from unittest.mock import patch

import pytest

from apps.billing.models import QboAccount, QboAccountMapping
from apps.integrations import qbo
from apps.payments import qbo_bills

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
