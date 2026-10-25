"""APC-42 — billing accounts and groups pushed to QuickBooks as customers / sub-customers.

Outbound only; the QuickBooks client (`integrations.qbo`, tested at the HTTP level in APC-41)
is mocked at its `request()` boundary. Nothing here calls Intuit.
"""

import json
from unittest.mock import patch

import pytest

from apps.billing import qbo_sync, services
from apps.billing.models import BillingAccount, QboTerm, SyncState, Terms
from apps.contacts.factories import ContactFactory
from apps.integrations import qbo
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
REQ = "apps.billing.qbo_sync.qbo.request"


@pytest.fixture(autouse=True)
def connected():
    with patch("apps.billing.qbo_sync.qbo.is_connected", return_value=True):
        yield


def _account(name="Ridgeline Partners", group="Accounts Payable", **kw):
    QboTerm.objects.get_or_create(qbo_id="3", defaults={"name": "Net 30", "active": True})
    return services.create_account(ContactFactory(), name=name, group_name=group, **kw)


def _created(qbo_id, token="0"):
    return {"Customer": {"Id": qbo_id, "SyncToken": token}}


def _fault(code, message="", status=400):
    body = json.dumps(
        {"Fault": {"Error": [{"code": code, "Message": message}], "type": "ValidationFault"}}
    )
    return qbo.QboAPIError(status, body)


def _net30():
    return QboTerm.objects.get_or_create(qbo_id="3", defaults={"name": "Net 30", "active": True})[0]


# --- the payloads -------------------------------------------------------------------------


def test_an_account_is_a_customer_that_bills_on_its_own_with_its_terms():
    _net30()
    account = _account(terms=Terms.NET_30)

    with patch(REQ, return_value=_created("58")) as req:
        qbo_sync.push(account)

    method, path = req.call_args.args
    body = req.call_args.kwargs["json"]
    assert (method, path) == ("POST", "customer")
    assert body["DisplayName"] == "Ridgeline Partners"
    assert body["BillWithParent"] is False
    assert body["SalesTermRef"] == {"value": "3"}
    assert "Id" not in body and "sparse" not in body


def test_a_group_is_a_sub_customer_of_its_account_with_its_own_email():
    account = _account()
    account.qbo_id = "58"
    account.qbo_sync_state = SyncState.SYNCED
    account.save()
    group = account.groups.get()
    group.invoice_email = "ap@ridgeline.test"
    group.save()

    with patch(REQ, return_value=_created("59")) as req:
        qbo_sync.push(group)

    body = req.call_args.kwargs["json"]
    # Plain group name: QuickBooks lets sub-customers of different accounts share a name
    # (Moe, 2026-10-25), and the indent under the account already shows whose it is.
    assert body["DisplayName"] == "Accounts Payable"
    assert body["ParentRef"] == {"value": "58"}
    assert body["Job"] is True
    assert body["BillWithParent"] is False
    assert body["PrimaryEmailAddr"] == {"Address": "ap@ridgeline.test"}


def test_a_term_quickbooks_doesnt_have_stops_the_push_with_a_clear_error():
    """Never silently change a customer's terms (ticket): a missing term is an error to fix
    in QuickBooks, not a customer pushed with no terms."""
    QboTerm.objects.create(qbo_id="9", name="Net 60", active=True)
    account = _account(terms=Terms.NET_45)

    with patch(REQ) as req:
        assert qbo_sync.push(account) is False

    req.assert_not_called()
    account.refresh_from_db()
    assert account.qbo_sync_state == SyncState.ERROR
    assert "Net 45" in account.qbo_sync_error
    assert "Net 45" in qbo_sync.unmatched_terms()


# --- create, update, idempotency ----------------------------------------------------------


def test_a_first_push_records_the_id_and_sync_token():
    account = _account()

    with patch(REQ, return_value=_created("58", "0")):
        assert qbo_sync.push(account) is True

    account.refresh_from_db()
    assert (account.qbo_id, account.qbo_sync_token, account.qbo_sync_state) == (
        "58",
        "0",
        SyncState.SYNCED,
    )
    assert account.qbo_synced_at is not None and account.qbo_sync_error == ""


def test_a_retried_create_sends_the_same_request_id_so_quickbooks_returns_the_original():
    account = _account()

    with patch(REQ, side_effect=[qbo.QboAPIError(0, "network: Timeout"), _created("58")]) as req:
        with pytest.raises(qbo_sync.SyncPaused):
            qbo_sync.push(account)
        qbo_sync.push(account)

    first, second = (c.kwargs["params"]["requestid"] for c in req.call_args_list)
    assert first == second and len(first) <= 50


def test_a_rename_is_a_sparse_update_with_the_stored_sync_token():
    account = _account()
    with patch(REQ, return_value=_created("58", "0")):
        qbo_sync.push(account)
    services.update_account(account, name="Ridgeline Partners LLC", terms=account.terms)

    with patch(REQ, return_value=_created("58", "1")) as req:
        qbo_sync.push(account)

    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["SyncToken"], body["sparse"]) == ("58", "0", True)
    assert body["DisplayName"] == "Ridgeline Partners LLC"
    account.refresh_from_db()
    assert account.qbo_sync_token == "1"


def test_a_stale_sync_token_is_re_read_once_and_retried():
    account = _account()
    with patch(REQ, return_value=_created("58", "0")):
        qbo_sync.push(account)
    services.update_account(account, name="Renamed", terms=account.terms)
    calls = [
        _fault("5010", "Stale Object Error"),
        {"Customer": {"Id": "58", "SyncToken": "4"}},
        _created("58", "5"),
    ]

    with patch(REQ, side_effect=calls) as req:
        assert qbo_sync.push(account) is True

    assert req.call_args_list[1].args == ("GET", "customer/58")
    assert req.call_args_list[2].kwargs["json"]["SyncToken"] == "4"


def test_running_the_sync_twice_sends_nothing_the_second_time():
    _account()

    with patch(REQ, side_effect=[_created("58"), _created("59")]) as req:
        qbo_sync.sync_pending()
        qbo_sync.sync_pending()

    assert req.call_count == 2  # the account, then its group — once each


# --- order and waiting ------------------------------------------------------------------------


def test_accounts_go_before_their_groups():
    _account()

    with patch(REQ, side_effect=[_created("58"), _created("59")]) as req:
        qbo_sync.sync_pending()

    first, second = (c.kwargs["json"] for c in req.call_args_list)
    assert "ParentRef" not in first and second["ParentRef"] == {"value": "58"}


def test_a_group_whose_account_failed_waits_and_never_errors_on_its_own():
    account = _account()

    with patch(REQ, side_effect=_fault("6240", "Duplicate Name Exists Error")) as req:
        qbo_sync.sync_pending()

    group = account.groups.get()
    assert req.call_count == 1  # the group was never sent
    assert (group.qbo_sync_state, group.qbo_sync_error) == (SyncState.NOT_SYNCED, "")


# --- failures --------------------------------------------------------------------------------


def test_a_duplicate_name_is_the_readable_message_and_a_notification():
    account = _account()

    with patch(REQ, side_effect=_fault("6240", "Duplicate Name Exists Error")):
        assert qbo_sync.push(account) is False

    account.refresh_from_db()
    assert account.qbo_sync_state == SyncState.ERROR
    assert account.qbo_sync_error == (
        "QuickBooks already has a customer with this name. Rename it here or there, then retry."
    )
    assert Notification.objects.filter(title__icontains="Ridgeline Partners").exists()


def test_a_failure_alerts_once_not_every_hour():
    account = _account()

    with patch(REQ, side_effect=_fault("6240")):
        qbo_sync.push(account)
        qbo_sync.push(account)

    assert Notification.objects.filter(title__icontains="Ridgeline").count() == 1


def test_an_outage_pauses_the_push_and_marks_nothing():
    account = _account()

    with patch(REQ, side_effect=qbo.QboAPIError(503, "down")):
        with pytest.raises(qbo_sync.SyncPaused):
            qbo_sync.push(account)

    account.refresh_from_db()
    assert (account.qbo_sync_state, account.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert not Notification.objects.exists()


def test_retry_sync_during_an_outage_says_to_try_again(cashier):
    account = _account()

    with patch(REQ, side_effect=qbo.QboAPIError(0, "network: Timeout")):
        resp = cashier.post(reverse("billing_account_sync", args=[account.pk]))

    assert resp.status_code == 502
    assert "try again" in resp.json()["error"].lower()


def test_nothing_is_sent_or_changed_while_quickbooks_is_not_connected():
    account = _account()

    with patch("apps.billing.qbo_sync.qbo.is_connected", return_value=False), patch(REQ) as req:
        assert qbo_sync.sync_pending() == 0

    req.assert_not_called()
    account.refresh_from_db()
    assert account.qbo_sync_state == SyncState.NOT_SYNCED


def test_a_connection_lost_mid_run_stops_the_run_and_marks_nothing():
    _account()
    _account(name="Second Co", group="Ops")

    with patch(REQ, side_effect=qbo.QboNotConnected("broken")) as req:
        qbo_sync.sync_pending()

    assert req.call_count == 1
    assert not Notification.objects.filter(title__icontains="sync failed").exists()


# --- terms ----------------------------------------------------------------------------------


def test_refreshing_terms_caches_quickbooks_terms_and_retires_missing_ones():
    QboTerm.objects.create(qbo_id="99", name="Old term", active=True)
    response = {
        "QueryResponse": {
            "Term": [
                {"Id": "1", "Name": "Due on receipt", "Active": True},
                {"Id": "3", "Name": "Net 30", "Active": True},
            ]
        }
    }

    with patch(REQ, return_value=response) as req:
        assert qbo_sync.refresh_terms() == 2

    assert req.call_args.args == ("GET", "query")
    assert "from Term" in req.call_args.kwargs["params"]["query"]
    assert set(QboTerm.objects.filter(active=True).values_list("name", flat=True)) == {
        "Due on receipt",
        "Net 30",
    }
    assert QboTerm.objects.get(qbo_id="99").active is False


def test_terms_match_by_name_ignoring_case():
    QboTerm.objects.create(qbo_id="7", name="NET 30", active=True)

    assert qbo_sync.term_ref(Terms.NET_30) == {"value": "7"}
    assert qbo_sync.term_ref(Terms.NET_15) is None


# --- Task 3: the cron, Retry sync, Settings --------------------------------------------------

from django.urls import reverse  # noqa: E402

from apps.accounts.factories import UserFactory  # noqa: E402
from apps.accounts.models import User  # noqa: E402
from apps.core.cron import JOBS  # noqa: E402


def test_the_hourly_job_refreshes_terms_then_pushes_whatever_is_pending():
    _account()
    terms = {"QueryResponse": {"Term": [{"Id": "3", "Name": "Net 30", "Active": True}]}}

    # APC-74: the job then reads the chart of accounts and pushes affiliate bills (none here).
    accounts = {"QueryResponse": {"Account": []}}
    with patch(REQ, side_effect=[terms, _created("58"), _created("59"), accounts]) as req:
        assert JOBS["retry-qbo-sync"]() == 2

    assert req.call_args_list[0].args == ("GET", "query")


def test_the_hourly_job_does_nothing_without_a_connection():
    _account()

    with patch("apps.billing.qbo_sync.qbo.is_connected", return_value=False), patch(REQ) as req:
        assert JOBS["retry-qbo-sync"]() == 0

    req.assert_not_called()


@pytest.fixture
def cashier(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


def test_retry_sync_pushes_an_account_now_and_answers_with_its_state(cashier):
    account = _account()
    account.qbo_sync_state = SyncState.ERROR
    account.save()

    with patch(REQ, return_value=_created("58")):
        resp = cashier.post(reverse("billing_account_sync", args=[account.pk]))

    assert resp.json() == {"ok": True, "state": "synced", "error": ""}


def test_retry_sync_on_a_group_reports_the_failure_in_words(cashier):
    account = _account()
    account.qbo_id = "58"
    account.save()
    group = account.groups.get()

    with patch(REQ, side_effect=_fault("6240")):
        resp = cashier.post(reverse("billing_group_sync", args=[group.pk]))

    body = resp.json()
    assert (body["ok"], body["state"]) == (True, "error")
    assert "already has a customer with this name" in body["error"]


def test_retry_sync_needs_payments_access_and_a_connection(client):
    account = _account()
    client.force_login(UserFactory())
    assert client.post(reverse("billing_account_sync", args=[account.pk])).status_code == 403

    client.force_login(UserFactory(can_manage_payments=True))
    with patch("apps.billing.qbo_sync.qbo.is_connected", return_value=False):
        resp = client.post(reverse("billing_account_sync", args=[account.pk]))
    assert resp.status_code == 400


def test_the_retry_button_is_wired_to_the_sync_endpoint(cashier, settings):
    account = _account()
    group = account.groups.get()
    for record in (account, group):
        record.qbo_sync_state = SyncState.ERROR
        record.qbo_sync_error = "QuickBooks couldn't be reached. It will try again within the hour."
        record.save()

    with patch("apps.billing.selectors._qbo_connected", return_value=True):
        html = cashier.get(reverse("contact_detail", args=[account.contact.pk])).content.decode()

    assert reverse("billing_account_sync", args=[account.pk]) in html
    assert reverse("billing_group_sync", args=[group.pk]) in html


def test_settings_lists_terms_quickbooks_lacks_and_can_refresh_them(client):
    from django.test import override_settings

    QboTerm.objects.create(qbo_id="3", name="Net 30", active=True)
    services.create_account(ContactFactory(), name="A", group_name="G", terms=Terms.NET_15)
    services.create_account(ContactFactory(), name="B", group_name="G", terms=Terms.NET_45)
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    with (
        override_settings(QBO_CLIENT_ID="id", QBO_CLIENT_SECRET="secret"),
        patch("apps.integrations.qbo.is_connected", return_value=True),
    ):
        html = client.get(reverse("quickbooks_settings")).content.decode()
        with (
            patch(REQ, return_value={"QueryResponse": {"Term": []}}),
            patch("apps.settings.views._qbo_or_404") as gate,
        ):
            gate.return_value = qbo
            with patch.object(qbo, "is_connected", return_value=True):
                resp = client.post(reverse("quickbooks_refresh_terms"))

    assert "Net 15" in html and "Net 45" in html  # in use, missing from QuickBooks
    assert (
        "Due on receipt" not in html.split("Not in QuickBooks yet")[-1].split("In QuickBooks:")[0]
    )
    assert "Refresh terms" in html
    assert resp.status_code == 302


def test_disconnecting_quickbooks_leaves_every_local_record_intact():
    account = _account()
    account.qbo_id = "58"
    account.qbo_sync_state = SyncState.SYNCED
    account.save()

    with patch.object(qbo.requests, "request"):
        qbo.disconnect()

    account.refresh_from_db()
    assert (account.qbo_id, account.qbo_sync_state) == ("58", SyncState.SYNCED)


# --- review fixes (2026-10-24) ----------------------------------------------------------------


def test_an_outage_mid_run_stops_the_run_and_marks_nothing():
    for n in range(3):
        _account(name=f"Co {n}", group=f"G{n}")

    with patch(REQ, side_effect=qbo.QboRateLimited("throttled", 30)) as req:
        assert qbo_sync.sync_pending() == 0

    assert req.call_count == 1
    assert not BillingAccount.objects.exclude(qbo_sync_state=SyncState.NOT_SYNCED).exists()
    assert not Notification.objects.exists()


def test_a_run_is_capped_so_a_big_first_sync_spreads_over_runs():
    for n in range(qbo_sync.SYNC_BATCH + 3):
        _account(name=f"Co {n}", group=f"G{n}")

    with patch(REQ, side_effect=lambda *a, **k: _created(str(100 + next(_ids)))):
        assert qbo_sync.sync_pending() == qbo_sync.SYNC_BATCH


import itertools  # noqa: E402

_ids = itertools.count()


def test_renaming_an_account_queues_its_groups_too():
    account = _account()
    with patch(REQ, side_effect=[_created("58"), _created("59")]):
        qbo_sync.sync_pending()

    services.update_account(account, name="Ridgeline Partners LLC", terms=account.terms)

    assert account.groups.get().qbo_sync_state == SyncState.NOT_SYNCED


def test_an_edit_saved_during_a_push_is_not_marked_synced():
    account = _account()

    def edited_meanwhile(*args, **kwargs):
        services.update_account(
            BillingAccount.objects.get(pk=account.pk),
            name="Renamed mid-flight",
            terms=account.terms,
        )
        return _created("58")

    with patch(REQ, side_effect=edited_meanwhile):
        qbo_sync.push(account)

    account.refresh_from_db()
    assert account.qbo_id == "58"  # the link is kept
    assert account.qbo_sync_state == SyncState.NOT_SYNCED  # the rename still goes out


def test_a_create_answered_with_an_older_version_stays_queued_for_an_update():
    """A replay of an earlier create (same requestid, edited since) returns the old record."""
    account = _account()

    with patch(
        REQ, return_value={"Customer": {"Id": "58", "SyncToken": "0", "DisplayName": "Old name"}}
    ):
        qbo_sync.push(account)

    account.refresh_from_db()
    assert account.qbo_id == "58"
    assert account.qbo_sync_state == SyncState.NOT_SYNCED


def test_after_a_definite_refusal_a_corrected_create_gets_a_new_request_id():
    account = _account()
    with patch(REQ, side_effect=_fault("6240")) as req:
        qbo_sync.push(account)
    first = req.call_args.kwargs["params"]["requestid"]
    services.update_account(account, name="Ridgeline Partners Two", terms=account.terms)

    with patch(REQ, return_value=_created("58")) as req:
        qbo_sync.push(account)

    assert req.call_args.kwargs["params"]["requestid"] != first


def test_clearing_a_groups_email_clears_it_in_quickbooks():
    account = _account()
    group = account.groups.get()
    group.invoice_email = "ap@ridgeline.test"
    group.save()
    with patch(REQ, side_effect=[_created("58"), _created("59")]):
        qbo_sync.sync_pending()
    services.update_group(group, name=group.name, invoice_email="")

    with patch(REQ, return_value=_created("59", "1")) as req:
        qbo_sync.push(account.groups.get())

    assert req.call_args.kwargs["json"]["PrimaryEmailAddr"] == {"Address": ""}


def test_records_from_another_quickbooks_company_are_pushed_again_as_new():
    account = _account()
    with (
        patch("apps.billing.qbo_sync.current_realm", return_value="111"),
        patch(REQ, side_effect=[_created("58"), _created("59")]),
    ):
        qbo_sync.sync_pending()

    with (
        patch("apps.billing.qbo_sync.current_realm", return_value="222"),
        patch(REQ, side_effect=[_created("7"), _created("8")]) as req,
    ):
        assert qbo_sync.sync_pending() == 2

    assert "Id" not in req.call_args_list[0].kwargs["json"]  # a create in the new company
    account.refresh_from_db()
    assert (account.qbo_id, account.qbo_realm_id) == ("7", "222")


def test_an_odd_fault_shape_is_still_an_error_not_a_crash():
    account = _account()
    odd = qbo.QboAPIError(400, json.dumps({"Fault": {"Error": {"code": "6000"}}}))

    with patch(REQ, side_effect=odd):
        assert qbo_sync.push(account) is False

    account.refresh_from_db()
    assert account.qbo_sync_state == SyncState.ERROR


def test_a_refusal_shows_intuits_reason():
    account = _account()
    with patch(REQ, side_effect=_fault("6000", "Invalid DisplayName: contains a colon")):
        qbo_sync.push(account)

    account.refresh_from_db()
    assert "contains a colon" in account.qbo_sync_error


def test_retry_sync_on_a_group_still_waiting_says_why(cashier):
    group = _account().groups.get()

    resp = cashier.post(reverse("billing_group_sync", args=[group.pk]))

    assert "account" in resp.json()["error"].lower()


def test_an_edit_in_the_admin_queues_the_record_and_its_groups(admin_client):
    account = _account()
    BillingAccount.objects.filter(pk=account.pk).update(
        qbo_id="58", qbo_sync_state=SyncState.SYNCED
    )
    account.groups.update(qbo_id="59", qbo_sync_state=SyncState.SYNCED)
    group = account.groups.get()

    admin_client.post(
        reverse("admin:billing_accountgroup_change", args=[group.pk]),
        {
            "account": account.pk,
            "name": "Renamed in admin",
            "invoice_email": "",
            "terms": "",
            "qbo_id": "59",
            "qbo_sync_token": "",
            "qbo_realm_id": "",
            "qbo_sync_state": SyncState.SYNCED,
            "qbo_sync_error": "",
        },
    )

    group.refresh_from_db()
    assert group.name == "Renamed in admin"
    assert group.qbo_sync_state == SyncState.NOT_SYNCED


def test_the_quickbooks_terms_cache_is_in_the_admin(admin_client):
    assert admin_client.get(reverse("admin:billing_qboterm_changelist")).status_code == 200


def test_two_accounts_can_each_have_an_accounts_payable_group():
    for n, name in enumerate(("Ridgeline Partners", "Harbor Events")):
        account = _account(name=name)
        account.qbo_id = str(58 + n)
        account.qbo_sync_state = SyncState.SYNCED
        account.save()

    with patch(REQ, side_effect=[_created("70"), _created("71")]) as req:
        qbo_sync.sync_pending()

    sent = [c.kwargs["json"] for c in req.call_args_list]
    assert [b["DisplayName"] for b in sent] == ["Accounts Payable", "Accounts Payable"]
    assert [b["ParentRef"]["value"] for b in sent] == ["58", "59"]


def test_a_failed_groups_alert_still_names_its_account():
    account = _account()
    account.qbo_id = "58"
    account.save()

    with patch(REQ, side_effect=_fault("6240")):
        qbo_sync.push(account.groups.get())

    assert Notification.objects.filter(
        title="QuickBooks sync failed: Ridgeline Partners - Accounts Payable"
    ).exists()
