"""APC-41 — the QuickBooks settings screen: connect, callback, disconnect. HTTP mocked."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.integrations import qbo
from apps.integrations.models import QboCredential

pytestmark = pytest.mark.django_db

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
    "QBO_REDIRECT_URI": "https://apc.example.com/integrations/quickbooks/callback/",
}


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    return client


def _live():
    cred = QboCredential.load()
    cred.realm_id = "9130"
    cred.set_tokens(
        access="at",
        refresh="rt",
        access_expires_at=timezone.now() + timedelta(hours=1),
        refresh_expires_at=None,
    )
    cred.connected_at = timezone.now()
    cred.save()
    return cred


@override_settings(**QBO)
def test_the_settings_screen_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    assert client.get(reverse("quickbooks_settings")).status_code in (302, 403)


@override_settings(**QBO)
def test_the_screen_offers_connect_when_not_connected(owner):
    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "Connect QuickBooks" in html
    assert "Sandbox" in html


@override_settings(**QBO)
def test_the_screen_shows_the_company_and_who_connected_it(owner):
    cred = _live()
    cred.connected_by = UserFactory(first_name="Moe", last_name="A")
    cred.save()

    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "9130" in html and "Moe A" in html
    assert "Disconnect" in html and "Reconnect" in html
    assert cred.refresh_token_enc not in html and cred.access_token_enc not in html


@override_settings(**QBO)
def test_a_broken_connection_says_so_and_offers_reconnect(owner):
    cred = _live()
    cred.broken_at = timezone.now()
    cred.broken_reason = "QuickBooks refused to renew the connection. Reconnect it in Settings."
    cred.save()

    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "refused to renew" in html and "Reconnect" in html


@override_settings(**QBO)
def test_connect_redirects_to_intuit_with_a_state_kept_in_the_session(owner):
    resp = owner.post(reverse("quickbooks_connect"))

    assert resp.status_code == 302
    assert resp["Location"].startswith("https://appcenter.intuit.com/connect/oauth2?")
    state = owner.session["qbo_oauth_state"]
    assert f"state={state}" in resp["Location"]


@override_settings(**QBO)
def test_the_callback_with_the_right_state_connects(owner):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo, "exchange_code") as exchange:
        exchange.return_value = _live()
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": "9130"},
        )

    assert resp.status_code == 302 and resp["Location"] == reverse("quickbooks_settings")
    assert exchange.call_args.args == ("c",)
    assert exchange.call_args.kwargs["realm_id"] == "9130"


@pytest.mark.parametrize("state", ["", "forged"])
@override_settings(**QBO)
def test_the_callback_refuses_a_missing_or_wrong_state(owner, state):
    owner.post(reverse("quickbooks_connect"))

    with patch.object(qbo, "exchange_code") as exchange:
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": "9130"},
        )

    assert resp.status_code == 400
    exchange.assert_not_called()


@override_settings(**QBO)
def test_the_callback_reports_a_declined_consent(owner):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo, "exchange_code") as exchange:
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"error": "access_denied", "state": state},
            follow=True,
        )

    from django.contrib.messages import get_messages

    exchange.assert_not_called()
    assert any("QuickBooks wasn't connected" in str(m) for m in get_messages(resp.wsgi_request))


@override_settings(**QBO)
def test_the_callback_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    resp = client.get(reverse("integrations:quickbooks_callback"), {"code": "c", "state": "s"})

    assert resp.status_code in (302, 403)


@override_settings(**QBO)
def test_disconnect_clears_the_connection(owner):
    _live()

    with patch.object(qbo.requests, "request"):
        resp = owner.post(reverse("quickbooks_disconnect"))

    assert resp.status_code == 302
    assert qbo.is_connected() is False


@override_settings(QBO_CLIENT_ID="", QBO_CLIENT_SECRET="")
def test_without_keys_the_tile_is_hidden_and_the_screen_404s(owner):
    index = owner.get(reverse("settings_index")).content.decode()

    assert "QuickBooks" not in index
    assert owner.get(reverse("quickbooks_settings")).status_code == 404


@override_settings(**QBO)
def test_with_keys_the_tile_shows_the_connection_state(owner):
    assert "Not connected" in owner.get(reverse("settings_index")).content.decode()
    _live()
    assert "Connected" in owner.get(reverse("settings_index")).content.decode()


@override_settings(**QBO)
def test_the_chrome_flag_follows_the_real_connection(owner):
    # Lazy, so pages that never read it pay no query.
    assert bool(owner.get(reverse("dashboard")).context["qbo_connected"]) is False
    _live()
    assert bool(owner.get(reverse("dashboard")).context["qbo_connected"]) is True


@pytest.mark.parametrize("realm", ["91x30", "1" * 33])
@override_settings(**QBO)
def test_the_callback_refuses_a_malformed_company_id(owner, realm):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo, "exchange_code") as exchange:
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": realm},
        )

    assert resp.status_code == 400
    exchange.assert_not_called()


@override_settings(**QBO)
def test_a_network_drop_mid_connect_is_a_message_not_a_500(owner):
    from django.contrib.messages import get_messages

    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo.requests, "request", side_effect=qbo.requests.Timeout("slow")):
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": "9130"},
        )

    assert resp.status_code == 302
    assert any("refused" in str(m) or "reach" in str(m) for m in get_messages(resp.wsgi_request))
    assert qbo.is_connected() is False


@override_settings(**QBO)
def test_a_state_works_only_once(owner):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]
    params = {"code": "c", "state": state, "realmId": "9130"}

    with patch.object(qbo, "exchange_code") as exchange:
        exchange.return_value = _live()
        owner.get(reverse("integrations:quickbooks_callback"), params)
        again = owner.get(reverse("integrations:quickbooks_callback"), params)

    assert again.status_code == 400
    assert exchange.call_count == 1


# --- APC-74: which accounts affiliate bills post to and pay from ----------------------------


def _cached_accounts(realm="9130"):
    from apps.billing.models import QboAccount

    expense = QboAccount.objects.create(
        realm_id=realm,
        qbo_id="70",
        name="Subcontracted transportation",
        account_type="Cost of Goods Sold",
    )
    bank = QboAccount.objects.create(
        realm_id=realm, qbo_id="35", name="Business checking", account_type="Bank"
    )
    return expense, bank


@override_settings(**QBO)
def test_the_screen_asks_for_both_accounts_until_they_are_chosen(owner):
    _live()
    _cached_accounts()

    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "Affiliate bills" in html
    assert 'name="expense_account"' in html and 'name="payment_account"' in html
    assert "data-tom" in html  # the searchable select, never a bare <select>
    assert "Subcontracted transportation" in html and "Business checking" in html
    assert "won't go to QuickBooks until" in html


@override_settings(**QBO)
def test_saving_the_account_choice(owner):
    from apps.billing.models import QboAccountMapping

    _live()
    expense, bank = _cached_accounts()

    resp = owner.post(
        reverse("quickbooks_accounts"),
        {"expense_account": expense.pk, "payment_account": bank.pk, "bills_from": "2026-11-01"},
    )

    assert resp.status_code == 302
    config = QboAccountMapping.load()
    assert (config.expense_account, config.payment_account) == (expense, bank)
    html = owner.get(reverse("quickbooks_settings")).content.decode()
    assert "won't go to QuickBooks until" not in html


@override_settings(**QBO)
def test_an_account_of_the_wrong_kind_or_company_is_refused(owner):
    from apps.billing.models import QboAccountMapping

    _live()
    expense, bank = _cached_accounts()
    _, theirs = _cached_accounts(realm="other")

    owner.post(
        reverse("quickbooks_accounts"),
        {"expense_account": bank.pk, "payment_account": theirs.pk},
    )

    config = QboAccountMapping.load()
    assert (config.expense_account, config.payment_account) == (None, None)


@override_settings(**QBO)
def test_refreshing_accounts_reads_the_chart_of_accounts(owner):
    _live()
    rows = {
        "QueryResponse": {
            "Account": [{"Id": "70", "Name": "Subs", "AccountType": "Expense", "Active": True}]
        }
    }

    with patch("apps.payments.qbo_bills.qbo.request", return_value=rows):
        resp = owner.post(reverse("quickbooks_refresh_accounts"))

    assert resp.status_code == 302
    from apps.billing.models import QboAccount

    assert QboAccount.objects.filter(realm_id="9130", name="Subs", active=True).exists()


@override_settings(**QBO)
def test_the_account_choice_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    assert client.post(reverse("quickbooks_accounts"), {}).status_code in (302, 403)


@override_settings(**QBO)
def test_saving_the_start_date_for_affiliate_bills(owner):
    from datetime import date

    from apps.billing.models import QboAccountMapping

    _live()
    expense, bank = _cached_accounts()

    owner.post(
        reverse("quickbooks_accounts"),
        {"expense_account": expense.pk, "payment_account": bank.pk, "bills_from": "2026-11-01"},
    )

    assert QboAccountMapping.load().bills_from == date(2026, 11, 1)
    html = owner.get(reverse("quickbooks_settings")).content.decode()
    assert 'name="bills_from"' in html and "data-flatpickr" in html
    assert 'value="2026-11-01"' in html


@override_settings(**QBO)
def test_without_a_start_date_the_screen_says_nothing_goes(owner):
    _live()
    expense, bank = _cached_accounts()
    owner.post(
        reverse("quickbooks_accounts"),
        {"expense_account": expense.pk, "payment_account": bank.pk, "bills_from": ""},
    )

    html = owner.get(reverse("quickbooks_settings")).content.decode()
    assert "won't go to QuickBooks until" in html
