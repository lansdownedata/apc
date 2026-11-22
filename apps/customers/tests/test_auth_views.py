"""The customer portal's sign-in, sign-up and password pages, and its shell."""

import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core import mail, signing
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.html import escape
from django.utils.http import urlsafe_base64_encode

from apps.audit.models import AuditEntry
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.customers import services, views
from apps.customers.auth import SESSION_KEY
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.customers.tokens import customer_reset_token, customer_token

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery"  # the factory's
NEW_PASSWORD = "a-long-pass-phrase-77"
SIGNUP = {"name": "Dana Whitfield", "email": "dana@example.com", "phone": "7035550188"}
CSRF = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')


def _login(client: Client, email: str, password: str, next_url: str = ""):
    url = reverse("customers:login") + (f"?{urlencode({'next': next_url})}" if next_url else "")
    return client.post(url, {"email": email, "password": password})


def _strip(content: bytes, *emails: str) -> bytes:
    """The page with its per-render CSRF token and the typed email taken out."""
    content = CSRF.sub(b"", content)
    for email in emails:
        content = content.replace(email.encode(), b"")
    return content


def _signup_token(data: dict = SIGNUP) -> str:
    return signing.dumps(data, salt=services.SIGNUP_SALT)


def _link(name: str, account: CustomerAccount, generator) -> str:
    return reverse(
        f"customers:{name}",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": generator.make_token(account),
        },
    )


def _set(client: Client, url: str, password: str = NEW_PASSWORD):
    """Follow a link's hop to its token-less address, then submit a password there."""
    hop = client.get(url)
    assert hop.status_code == 302
    return client.post(hop.url, {"new_password1": password, "new_password2": password})


# --- sign in / out ---------------------------------------------------------------------


def test_login_signs_in_and_lands_on_home(client, account):
    response = _login(client, account.email.upper(), PASSWORD)
    assert response.status_code == 302
    assert response.url == reverse("customers:home")
    assert client.session[SESSION_KEY] == account.pk
    home = client.get(response.url)
    assert home.status_code == 200
    assert "Sign out" in home.content.decode()


def test_the_sign_in_page_matches_the_mockup(client):
    html = client.get(reverse("customers:login")).content.decode()
    assert "Sign in to your account" in html
    assert "Forgot password?" in html
    assert "First time here?" in html
    assert reverse("customers:signup") in html
    assert reverse("customers:password_reset") in html
    assert "Sign out" not in html  # the signed-in nav is not on a signed-out page


@pytest.mark.parametrize("name", ["customers:login", "customers:signup"])
def test_a_signed_in_customer_skips_sign_in_and_sign_up(client_as, account, name):
    client = client_as(account)
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert response.url == reverse("customers:home")
    nexted = client.get(reverse(name), {"next": "/account/trips/"})
    assert nexted.url == "/account/trips/"
    assert client.get(reverse(name), {"next": "/portal/"}).url == reverse("customers:home")


@pytest.mark.parametrize("name", ["customers:login", "customers:signup"])
def test_a_signed_out_visitor_sees_the_sign_in_and_sign_up_forms(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 200
    assert 'name="email"' in response.content.decode()


def test_wrong_password_shows_one_generic_error(client, account):
    wrong = _login(client, account.email, "not the password")
    unknown = _login(client, "nobody@example.com", "not the password")
    assert wrong.status_code == unknown.status_code == 200
    assert "That email and password don't match." in wrong.content.decode()
    assert _strip(wrong.content, account.email) == _strip(unknown.content, "nobody@example.com")
    assert SESSION_KEY not in client.session


def test_a_pending_invite_cannot_sign_in(client):
    invited = CustomerAccountFactory(invited=True)
    response = _login(client, invited.email, PASSWORD)
    assert response.status_code == 200
    assert SESSION_KEY not in client.session


def test_sign_out_is_post_only_and_ends_the_session(client_as, account):
    client = client_as(account)
    assert client.get(reverse("customers:logout")).status_code == 405
    response = client.post(reverse("customers:logout"))
    assert response.status_code == 302
    assert response.url == reverse("customers:login")
    assert client.get(reverse("customers:home")).status_code == 302


def test_home_sends_a_signed_out_visitor_to_sign_in(client):
    response = client.get(reverse("customers:home"))
    assert response.status_code == 302
    assert response.url == f"{reverse('customers:login')}?next=/account/"


def test_a_next_under_account_is_honoured(client, account):
    response = _login(client, account.email, PASSWORD, next_url="/account/trips/")
    assert response.url == "/account/trips/"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test",
        "//evil.test/account/",
        "/portal/",
        "/planner/",
        "/account/../portal/",
        # browsers read %2e as "." and "\\" as "/", so each of these lands outside /account/
        "/account/%2e%2e/portal/",
        "/account/.%2E/planner/",
        "/account/..\\portal/",
        "/account/\\..\\portal/",
    ],
)
def test_next_outside_account_is_ignored(client, account, target):
    response = _login(client, account.email, PASSWORD, next_url=target)
    assert response.status_code == 302
    assert response.url == reverse("customers:home")


def test_eleventh_login_attempt_from_one_ip_is_refused(client, db):
    account = CustomerAccountFactory()
    for _ in range(10):
        _login(client, "nobody@example.com", "wrong")
    response = _login(client, account.email, PASSWORD)
    assert response.status_code == 429
    assert "Too many attempts." in response.content.decode()
    assert SESSION_KEY not in client.session
    other_ip = Client(REMOTE_ADDR="10.0.0.9")
    assert _login(other_ip, account.email, PASSWORD).status_code == 302


# --- sign up ---------------------------------------------------------------------------


def test_the_sign_up_page_matches_the_mockup(client):
    html = client.get(reverse("customers:signup")).content.decode()
    assert "Create your account" in html
    assert "Use the email you book with and we'll bring in your past trips." in html
    for label in ("Name", "Email", "Mobile", "Continue"):
        assert label in html


def test_signup_response_is_identical_for_known_and_unknown_email(client, db, outbound_on):
    existing = CustomerAccountFactory()
    url = reverse("customers:signup")
    known = client.post(url, {"name": "A", "email": existing.email, "phone": ""})
    unknown = client.post(url, {"name": "A", "email": "nobody@example.com", "phone": ""})
    assert known.status_code == unknown.status_code == 200
    assert _strip(known.content, existing.email) == _strip(unknown.content, "nobody@example.com")
    assert "Check your email" in unknown.content.decode()
    assert "nobody@example.com" in unknown.content.decode()
    # what was mailed differs (a reset for the known address); the page never says so
    assert [m.to for m in mail.outbox] == [[existing.email], ["nobody@example.com"]]
    assert Contact.objects.filter(email="nobody@example.com").count() == 0


def test_signup_while_sending_is_off_says_to_call(client, settings):
    settings.OUTBOUND_ENABLED = False
    response = client.post(reverse("customers:signup"), SIGNUP)
    assert response.status_code == 200
    assert escape("We can't set up accounts online right now.") in response.content.decode()
    assert mail.outbox == []


def test_signup_needs_an_email(client, outbound_on):
    response = client.post(reverse("customers:signup"), {**SIGNUP, "email": ""})
    assert response.status_code == 200
    assert "Check your email" not in response.content.decode()
    assert mail.outbox == []


def test_signup_counts_against_the_reset_limit(client, outbound_on):
    for i in range(5):
        client.post(reverse("customers:password_reset"), {"email": f"p{i}@example.com"})
    response = client.post(reverse("customers:signup"), SIGNUP)
    assert response.status_code == 200
    assert "Check your email" in response.content.decode()  # the same answer, nothing sent
    assert mail.outbox == []


def test_signup_confirm_sets_password_and_signs_in(client, db):
    hop = client.get(reverse("customers:signup_confirm", args=[_signup_token()]))
    # like Django's reset view: the token moves into the session and out of the address bar
    assert hop.status_code == 302
    assert hop.url == reverse("customers:signup_confirm", args=[views.TOKEN_PLACEHOLDER])
    page = client.get(hop.url)
    assert page.status_code == 200
    html = page.content.decode()
    assert "Set your password" in html and "dana@example.com" in html
    assert "At least 10 characters." in html and "Save and sign in" in html

    response = client.post(hop.url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert response.status_code == 302
    assert response.url == reverse("customers:home")
    account = CustomerAccount.objects.get(contact__email="dana@example.com")
    assert account.status == CustomerAccount.Status.ACTIVE and account.self_registered
    assert account.check_password(NEW_PASSWORD)
    assert client.session[SESSION_KEY] == account.pk
    assert client.get(reverse("customers:home")).status_code == 200


def test_a_short_password_is_refused_with_the_mockups_rule(client, db):
    hop = client.get(reverse("customers:signup_confirm", args=[_signup_token()])).url
    response = client.post(hop, {"new_password1": "short-pw1", "new_password2": "short-pw1"})
    assert response.status_code == 200
    assert "at least 10 characters" in response.content.decode()
    assert CustomerAccount.objects.count() == 0


def test_signup_through_the_view_audits_the_new_contact_as_the_customer_portal(client, db):
    _set(client, reverse("customers:signup_confirm", args=[_signup_token()]))
    contact = Contact.objects.get(email="dana@example.com")
    entry = AuditEntry.objects.get(
        content_type=ContentType.objects.get_for_model(Contact),
        object_id=contact.pk,
        action=AuditEntry.Action.CREATE,
    )
    assert entry.source == "customer portal"
    assert entry.actor is None


# --- links -----------------------------------------------------------------------------


def test_an_invite_link_registers_and_signs_in(client):
    invited = CustomerAccountFactory(invited=True)
    response = _set(client, _link("register", invited, customer_token))
    assert response.url == reverse("customers:home")
    invited.refresh_from_db()
    assert invited.status == CustomerAccount.Status.ACTIVE
    assert client.session[SESSION_KEY] == invited.pk


def test_a_reset_link_sets_a_new_password_and_signs_in(client, account):
    url = _link("set_password", account, customer_reset_token)
    hop = client.get(url)
    assert customer_reset_token.make_token(account) not in hop.url
    page = client.get(hop.url).content.decode()
    assert "Choose a new password" in page and account.email in page
    response = client.post(hop.url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
    assert response.url == reverse("customers:home")
    account.refresh_from_db()
    assert account.check_password(NEW_PASSWORD)
    assert client.session[SESSION_KEY] == account.pk


def test_the_token_less_address_needs_the_session_token(client, account):
    hop = client.get(_link("set_password", account, customer_reset_token)).url
    assert Client().get(hop).status_code == 400
    signup_hop = client.get(reverse("customers:signup_confirm", args=[_signup_token()])).url
    assert Client().get(signup_hop).status_code == 400


def test_bad_or_expired_link_renders_link_invalid(client, account):
    def refused(url: str) -> None:
        response = client.get(url, follow=True)
        assert response.status_code == 400
        assert "customers/link_invalid.html" in [t.name for t in response.templates]

    refused(reverse("customers:signup_confirm", args=[signing.dumps(SIGNUP, salt="other")]))
    token = _signup_token()
    later = timezone.now().timestamp() + timedelta(days=7, seconds=1).total_seconds()
    with patch("django.core.signing.time.time", return_value=later):
        refused(reverse("customers:signup_confirm", args=[token]))
    bogus = {"uidb64": "MTIz", "token": "nope"}
    refused(reverse("customers:set_password", kwargs=bogus))
    refused(reverse("customers:register", kwargs=bogus))
    # an invite link is not a reset link
    refused(_link("set_password", account, customer_token))
    revoked = CustomerAccountFactory(invited=True)
    url = _link("register", revoked, customer_token)
    services.revoke(revoked, actor=None)
    refused(url)


def test_a_used_reset_link_is_refused(client, account):
    url = _link("set_password", account, customer_reset_token)
    _set(client, url)
    assert client.get(url).status_code == 400


def test_a_used_signup_link_shows_why(client, db):
    url = reverse("customers:signup_confirm", args=[_signup_token()])
    _set(client, url)
    again = _set(Client(), url)
    assert again.status_code == 400
    assert escape("This link has already been used.") in again.content.decode()


def test_a_revoked_customers_signup_link_shows_the_call_us_message(client):
    revoked = CustomerAccountFactory(revoked=True, contact=ContactFactory(email="dana@example.com"))
    response = _set(client, reverse("customers:signup_confirm", args=[_signup_token()]))
    assert response.status_code == 400
    assert "This account is closed." in response.content.decode()
    revoked.refresh_from_db()
    assert revoked.status == CustomerAccount.Status.REVOKED


# --- forgot password -------------------------------------------------------------------


def test_password_reset_answers_the_same_and_mails_only_an_account(client, account, outbound_on):
    url = reverse("customers:password_reset")
    known = client.post(url, {"email": account.email})
    unknown = client.post(url, {"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert _strip(known.content, account.email) == _strip(unknown.content, "nobody@example.com")
    assert "Check your email" in known.content.decode()
    assert [m.to for m in mail.outbox] == [[account.email]]


# --- the shell -------------------------------------------------------------------------


def test_pages_are_noindex_and_no_store(client):
    token = _signup_token()
    hop = client.get(reverse("customers:signup_confirm", args=[token])).url
    for url in (
        reverse("customers:login"),
        reverse("customers:signup"),
        reverse("customers:password_reset"),
        hop,
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert '<meta name="robots" content="noindex, nofollow">' in response.content.decode()
        assert "no-store" in response["Cache-Control"], url


def test_the_signed_in_shell_has_the_portal_nav(client_as, account, settings):
    settings.COMPANY_PHONE = "(703) 555-0142"
    response = client_as(account).get(reverse("customers:home"))
    html = response.content.decode()
    assert "no-store" in response["Cache-Control"]
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert "(703) 555-0142" in html
    assert 'href="/account/"' in html
    assert reverse("customers:logout") in html
    assert "Service Area" not in html  # no marketing footer


def test_public_header_links_to_customer_login(client):
    html = client.get("/").content.decode()
    header, _, rest = html.partition("</header>")
    footer = rest[rest.index("<footer") :]
    login = f'href="{reverse("customers:login")}"'
    assert login in header and login in footer
    assert "Customer login" in header and "Customer login" in footer
