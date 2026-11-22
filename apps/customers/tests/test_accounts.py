"""Customer account services: sign-in check, invite, self sign-up, password reset, and the
three emails they send."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail, signing
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.core import outbound
from apps.core.choices import Channel
from apps.customers import services
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.customers.tokens import customer_reset_token, customer_token
from apps.messaging import defaults
from apps.messaging.models import Delivery, MessageTemplate, TouchPoint
from apps.planners.selectors import dispatch_phone

pytestmark = pytest.mark.django_db

PW = "a-long-pass-phrase-77"
SIGNUP = {"name": "Dana Whitfield", "email": "dana@example.com", "phone": "7035550188"}


def _signup_token(message) -> str:
    """The token in the sign-up link a mail carries."""
    prefix = "https://example.test/account/signup/"
    start = message.body.index(prefix) + len(prefix)
    return message.body[start:].split("/", 1)[0]


# --- the source channel ---------------------------------------------------------------


def test_customer_portal_is_a_source_channel():
    assert Channel.PORTAL == "portal"
    assert Channel.PORTAL.label == "Customer portal"


# --- the three message rows -----------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "subject"),
    [
        ("customer_invite", "Your {company_name} account"),
        ("customer_signup", "Finish creating your {company_name} account"),
        ("customer_password_reset", "Reset your {company_name} password"),
    ],
)
def test_each_email_is_a_seeded_customer_row(key, subject):
    row = MessageTemplate.objects.get(key=key)
    assert row.seeded is True and row.enabled is True
    assert (row.audience, row.trigger, row.subject) == ("customer", key, subject)
    assert (row.send_sms, row.send_email) == (False, True)
    assert "{account_button}" in row.email_body
    assert row.email_body == defaults.INVITES[key].email_body


def test_the_link_pages_resolve():
    assert reverse("customers:register", args=["MQ", "abc-def"]) == (
        "/account/register/MQ/abc-def/"
    )
    assert reverse("customers:signup_confirm", args=["tok:en"]) == "/account/signup/tok:en/"
    assert reverse("customers:set_password", args=["MQ", "abc-def"]) == (
        "/account/password/MQ/abc-def/"
    )


# --- self sign-up ---------------------------------------------------------------------


def test_start_signup_makes_no_contact_or_account_and_logs_only_the_mail(outbound_on):
    services.start_signup(name="Dana", email="dana@example.com", phone="7035550188")
    assert Contact.objects.count() == 0 and CustomerAccount.objects.count() == 0
    assert len(mail.outbox) == 1
    # the mail leaves the trail every send leaves, holding the address that was typed
    (delivery,) = Delivery.objects.filter(touchpoint__kind="customer_signup")
    assert (delivery.address, delivery.status) == ("dana@example.com", Delivery.Status.SENT)


def test_a_name_reaches_the_link_without_control_characters(outbound_on):
    services.start_signup(name="Dana\r\nBcc: x@evil.test", email="dana@example.com", phone="")
    data = services.read_signup(_signup_token(mail.outbox[0]))
    assert data["name"] == "Dana Bcc: x@evil.test"
    account = services.complete_signup({**data, "name": "Dana\x00\x1b Whitfield"}, PW)
    assert account.contact.name == "Dana Whitfield"


def test_start_signup_emails_a_signed_link_carrying_the_form(outbound_on):
    services.start_signup(name=" Dana Whitfield ", email=" Dana@Example.com ", phone="7035550188")
    (message,) = mail.outbox
    assert message.to == ["dana@example.com"]
    assert message.subject == "Finish creating your All Pro Charter account"
    assert "Hi Dana," in message.body
    assert "Use the button below to set your password and sign in." in message.body
    assert services.read_signup(_signup_token(message)) == {
        "name": "Dana Whitfield",
        "email": "dana@example.com",
        "phone": "7035550188",
    }
    tp = TouchPoint.objects.get(kind="customer_signup")
    assert tp.lead is None


def test_signup_for_active_account_sends_reset_not_signup(outbound_on):
    account = CustomerAccountFactory(contact=ContactFactory(email="dana@example.com"))
    result = services.start_signup(name="Dana", email="DANA@example.com", phone="")
    assert result is None
    (message,) = mail.outbox
    assert message.subject == "Reset your All Pro Charter password"
    assert "/account/password/" in message.body
    assert "/account/signup/" not in message.body
    assert TouchPoint.objects.filter(kind="customer_signup").count() == 0
    assert CustomerAccount.objects.get() == account


def test_start_signup_is_the_same_for_a_known_contact_and_a_stranger(outbound_on):
    ContactFactory(email="known@example.com")
    assert services.start_signup(name="K", email="known@example.com", phone="") is None
    assert services.start_signup(name="S", email="stranger@example.com", phone="") is None
    assert [m.subject for m in mail.outbox] == [
        "Finish creating your All Pro Charter account",
        "Finish creating your All Pro Charter account",
    ]


def test_start_signup_is_refused_while_outbound_is_off(settings):
    settings.OUTBOUND_ENABLED = False
    settings.PUBLIC_BASE_URL = "https://example.test"
    with pytest.raises(services.CustomerError):
        services.start_signup(**SIGNUP)
    assert mail.outbox == []


def test_start_signup_goes_quiet_after_three_mails_an_hour(outbound_on):
    for _ in range(4):
        services.start_signup(**SIGNUP)
        cache.clear()  # the cap is not kept per web worker: each one reads the same count
    assert len(mail.outbox) == 3
    with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=61)):
        services.start_signup(**SIGNUP)
    assert len(mail.outbox) == 4


def test_signup_link_expires_after_seven_days(outbound_on):
    services.start_signup(**SIGNUP)
    token = _signup_token(mail.outbox[0])
    later = timezone.now().timestamp() + timedelta(days=7, seconds=1).total_seconds()
    with patch("django.core.signing.time.time", return_value=later):
        with pytest.raises(signing.SignatureExpired):
            services.read_signup(token)
    assert services.read_signup(token)["email"] == "dana@example.com"


def test_a_tampered_signup_link_is_refused():
    with pytest.raises(signing.BadSignature):
        services.read_signup(signing.dumps(SIGNUP, salt="something.else"))


def test_signup_creates_a_portal_contact_for_a_new_email():
    account = services.complete_signup(SIGNUP, PW)
    contact = account.contact
    assert (contact.name, contact.email, contact.channel) == (
        "Dana Whitfield",
        "dana@example.com",
        Channel.PORTAL,
    )
    assert contact.phone == "+17035550188"
    assert account.registered_at is not None
    assert account.self_registered is True
    assert account.check_password(PW)
    assert account.can_sign_in


def test_signup_attaches_to_existing_contact_case_insensitively():
    contact = ContactFactory(name="Dana Whitfield", email="dana@example.com")
    account = services.complete_signup(
        {"name": "D W", "email": " Dana@Example.COM ", "phone": ""}, PW
    )
    assert account.contact == contact
    contact.refresh_from_db()
    assert contact.name == "Dana Whitfield"
    assert Contact.objects.count() == 1


def test_signup_adds_a_new_mobile_as_an_extra_number():
    contact = ContactFactory(email="dana@example.com", phone="+15715550142")
    services.complete_signup(SIGNUP, PW)
    contact.refresh_from_db()
    assert contact.phone == "+15715550142"  # the texting number stays put
    assert set(contact.phones.values_list("number", flat=True)) == {
        "+15715550142",
        "+17035550188",
    }


def test_signup_never_matches_by_phone():
    other = ContactFactory(email="other@example.com", phone="+17035550188")
    account = services.complete_signup(
        {"name": "N", "email": "new@example.com", "phone": "7035550188"}, PW
    )
    assert account.contact != other
    assert account.contact.email == "new@example.com"


def test_signup_completes_an_invited_account_instead_of_duplicating_it():
    invited = CustomerAccountFactory(invited=True, contact=ContactFactory(email="dana@example.com"))
    account = services.complete_signup(SIGNUP, PW)
    assert account.pk == invited.pk
    assert CustomerAccount.objects.count() == 1
    assert account.status == CustomerAccount.Status.ACTIVE
    assert account.self_registered is True


def test_used_signup_link_is_refused():
    services.complete_signup(SIGNUP, PW)
    with pytest.raises(services.CustomerError, match="This link has already been used."):
        services.complete_signup(SIGNUP, "another-long-phrase-88")
    assert CustomerAccount.objects.get().check_password(PW)


def test_a_double_submitted_link_with_a_new_email_is_refused_not_crashed():
    """The second request didn't see the first's new contact when it looked."""
    services.complete_signup(SIGNUP, PW)
    with patch.object(Contact.objects, "find_match", return_value=None):
        with pytest.raises(services.CustomerError, match="This link has already been used."):
            services.complete_signup(SIGNUP, "another-long-phrase-88")
    assert Contact.objects.count() == 1
    assert CustomerAccount.objects.get().check_password(PW)


def test_a_double_submitted_link_for_a_known_contact_is_refused_not_crashed():
    """The second request didn't see the first's new account when it looked."""
    ContactFactory(email="dana@example.com")
    services.complete_signup(SIGNUP, PW)
    nothing = CustomerAccount.objects.none()
    with patch.object(CustomerAccount.objects, "select_for_update", return_value=nothing):
        with pytest.raises(services.CustomerError, match="This link has already been used."):
            services.complete_signup(SIGNUP, "another-long-phrase-88")
    assert CustomerAccount.objects.get().check_password(PW)


# --- invite, resend, revoke -----------------------------------------------------------


def test_invite_creates_a_pending_account_and_emails_the_set_password_link(outbound_on):
    agent = UserFactory()
    contact = ContactFactory(name="Dana Whitfield", email="Dana@Example.com", phone="")
    account, sent = services.invite(contact, actor=agent)
    assert sent is True
    assert account.status == CustomerAccount.Status.INVITED
    assert (account.invited_by, account.self_registered) == (agent, False)
    assert account.has_usable_password() is False
    (message,) = mail.outbox
    assert message.to == ["dana@example.com"]
    assert message.subject == "Your All Pro Charter account"
    assert "Hi Dana," in message.body
    assert "/account/register/" in message.body


def test_invite_refused_while_outbound_off(settings):
    settings.OUTBOUND_ENABLED = False
    settings.PUBLIC_BASE_URL = "https://example.test"
    with pytest.raises(services.CustomerError) as exc:
        services.invite(ContactFactory(), actor=UserFactory())
    assert str(exc.value) == outbound.OFF_MESSAGE
    assert not CustomerAccount.objects.exists()


def test_invite_refused_while_public_base_url_is_blank(settings):
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = ""
    with pytest.raises(services.CustomerError, match="PUBLIC_BASE_URL"):
        services.invite(ContactFactory(), actor=UserFactory())
    assert not CustomerAccount.objects.exists()


@pytest.mark.parametrize("send", ["resend", "reset"])
def test_a_blank_public_address_refuses_every_send_in_words_that_fit_it(settings, send):
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = ""
    account = CustomerAccountFactory(invited=True) if send == "resend" else CustomerAccountFactory()
    action = services.resend if send == "resend" else services.staff_password_reset
    with pytest.raises(services.CustomerError) as refused:
        action(account)
    assert str(refused.value) == services.NO_BASE_URL
    assert "invite" not in services.NO_BASE_URL
    assert mail.outbox == []


def test_resend_is_refused_once_the_company_became_an_affiliate(outbound_on):
    account, _ = services.invite(ContactFactory(company=CompanyFactory()), actor=UserFactory())
    invited_at = account.invited_at
    account.contact.company.is_affiliate_account = True
    account.contact.company.save(update_fields=["is_affiliate_account"])
    mail.outbox.clear()
    with pytest.raises(services.CustomerError, match=services.AFFILIATE_REFUSED):
        services.resend(account)
    account.refresh_from_db()
    assert account.invited_at == invited_at  # the old link was not killed for nothing
    assert mail.outbox == []


@pytest.mark.parametrize(
    ("contact", "action", "reason"),
    [
        # affiliate first (it will not change), then no email, then the switch, then config
        ({"email": None, "affiliate": True}, "invite", services.AFFILIATE_REFUSED),
        ({"email": None, "affiliate": True}, "resend", services.AFFILIATE_REFUSED),
        ({"email": None, "affiliate": True}, "reset", "Add an email address for Dana first."),
        ({"email": None}, "invite", "Add an email address for Dana before inviting them."),
        ({}, "invite", outbound.OFF_MESSAGE),
    ],
)
def test_send_blocked_names_one_reason_in_one_order(settings, contact, action, reason):
    settings.OUTBOUND_ENABLED = False
    settings.PUBLIC_BASE_URL = ""
    company = CompanyFactory(is_affiliate_account=bool(contact.pop("affiliate", False)))
    person = ContactFactory(name="Dana", company=company, **contact)
    assert services.send_blocked(person, action=action) == reason


def test_the_invite_claims_no_history_and_gives_the_dispatch_number(outbound_on, settings):
    settings.COMPANY_PHONE = "7035550142"
    services.invite(ContactFactory(name="Dana Whitfield"), actor=UserFactory())
    body = mail.outbox[0].body
    assert "Any past trips with us are already in your account." in body
    assert "Your past trips" not in body
    # the number the portal's own pages show, written the way they write it
    assert "Questions? Call (703) 555-0142." in body
    assert dispatch_phone()["display"] == "(703) 555-0142"


def test_account_mail_never_prints_a_number_nobody_can_call(outbound_on, settings):
    settings.COMPANY_PHONE = "5555555555"  # not a real number: the pages fall back, so does mail
    services.invite(ContactFactory(), actor=UserFactory())
    assert "5555555555" not in mail.outbox[0].body
    assert f"Questions? Call {dispatch_phone()['display']}." in mail.outbox[0].body


def test_invite_needs_contact_email(outbound_on):
    with pytest.raises(services.CustomerError, match="email"):
        services.invite(ContactFactory(email=None), actor=UserFactory())
    assert not CustomerAccount.objects.exists()


def test_invite_refuses_an_active_or_already_invited_account(outbound_on, account):
    with pytest.raises(services.CustomerError, match="already"):
        services.invite(account.contact, actor=UserFactory())
    invited = CustomerAccountFactory(invited=True)
    with pytest.raises(services.CustomerError, match="Resend"):
        services.invite(invited.contact, actor=UserFactory())


def test_resend_kills_previous_invite_link(outbound_on):
    account, _ = services.invite(ContactFactory(), actor=UserFactory())
    old = customer_token.make_token(account)
    with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
        assert services.resend(account) is True
    account.refresh_from_db()
    assert customer_token.check_token(account, old) is False
    assert len(mail.outbox) == 2


def test_resend_only_resends_a_pending_invite(outbound_on, account):
    with pytest.raises(services.CustomerError):
        services.resend(account)


def test_revoke_then_invite_reopens_account(outbound_on, account):
    agent = UserFactory()
    link = customer_token.make_token(account)
    services.revoke(account, actor=agent)
    account.refresh_from_db()
    assert account.status == CustomerAccount.Status.REVOKED
    assert account.revoked_by == agent
    assert customer_token.check_token(account, link) is False
    assert services.authenticate(account.email, "correct horse battery") is None

    reopened, sent = services.invite(account.contact, actor=agent)
    assert sent is True and reopened.pk == account.pk
    assert reopened.status == CustomerAccount.Status.INVITED
    services.register(reopened, PW)
    assert services.authenticate(account.email, PW) == reopened


def test_register_sets_the_password_and_clears_a_lock():
    account = CustomerAccountFactory(
        invited=True, failed_logins=4, locked_until=timezone.now() + timedelta(minutes=5)
    )
    services.register(account, PW)
    account.refresh_from_db()
    assert account.status == CustomerAccount.Status.ACTIVE
    assert account.check_password(PW)
    assert (account.failed_logins, account.locked_until) == (0, None)
    assert account.self_registered is False


# --- sign-in --------------------------------------------------------------------------


def test_authenticate_accepts_the_password_only(account):
    assert services.authenticate(account.email.upper(), "correct horse battery") == account
    assert services.authenticate(account.email, "wrong") is None
    assert services.authenticate("nobody@example.com", "correct horse battery") is None
    assert services.authenticate(CustomerAccountFactory(invited=True).email, "x") is None


def test_authenticate_uses_new_email_after_staff_edit(account):
    old = account.email
    contact = account.contact
    contact.email = "Dana.New@Example.com"
    contact.save(update_fields=["email", "updated_at"])
    assert services.authenticate("dana.new@example.com", "correct horse battery") == account
    assert services.authenticate(old, "correct horse battery") is None


def test_a_right_password_clears_the_failed_count(account):
    for _ in range(3):
        services.authenticate(account.email, "wrong-password-1")
    account.refresh_from_db()
    assert account.failed_logins == 3
    assert services.authenticate(account.email, "correct horse battery") == account
    account.refresh_from_db()
    assert (account.failed_logins, account.locked_until) == (0, None)


def test_wrong_guesses_sent_side_by_side_are_each_counted(account):
    """Ten requests in flight together each read the account before any of them counted.
    Counting from that copy, all ten would write "1" and the lock would never come."""
    copies = [CustomerAccount.objects.get(pk=account.pk) for _ in range(services.LOCKOUT_FAILURES)]
    now = timezone.now()
    for copy in copies:
        services._record_failure(copy, now)
    account.refresh_from_db()
    assert account.locked_until == now + services.LOCKOUT_FOR


def test_ten_wrong_passwords_lock_for_fifteen_minutes(account):
    for _ in range(services.LOCKOUT_FAILURES):
        assert services.authenticate(account.email, "wrong-password-1") is None
    account.refresh_from_db()
    assert account.locked_until is not None
    assert services.authenticate(account.email, "correct horse battery") is None
    later = timezone.now() + timedelta(minutes=15, seconds=1)
    with patch("django.utils.timezone.now", return_value=later):
        assert services.authenticate(account.email, "correct horse battery") == account
    account.refresh_from_db()
    assert (account.failed_logins, account.locked_until) == (0, None)


# --- password reset and change --------------------------------------------------------


def test_password_reset_emails_an_active_account(outbound_on, account):
    assert services.send_password_reset(account.email.upper()) is True
    (message,) = mail.outbox
    assert message.subject == "Reset your All Pro Charter password"
    assert "/account/password/" in message.body
    assert "Choose a new password" in message.alternatives[0][0]


def test_password_reset_is_silent_for_an_unknown_email_and_still_renders(outbound_on):
    with patch("apps.messaging.rendering.render_email") as render:
        assert services.send_password_reset("nobody@example.com") is False
    render.assert_called_once()
    assert mail.outbox == []


def test_password_reset_goes_quiet_after_three_an_hour(outbound_on, account):
    results = [services.send_password_reset(account.email) for _ in range(4)]
    assert results == [True, True, True, False]
    assert len(mail.outbox) == 3


def test_a_staff_reset_past_the_cap_says_the_cap_is_why(outbound_on, account):
    for _ in range(services.MAILS_PER_EMAIL):
        assert services.staff_password_reset(account) is True
    with pytest.raises(services.CustomerError) as refused:
        services.staff_password_reset(account)
    assert str(refused.value) == services.RESET_CAPPED
    assert len(mail.outbox) == services.MAILS_PER_EMAIL


def test_a_staff_reset_that_fails_to_deliver_does_not_blame_the_cap(outbound_on, account):
    with patch("apps.notifications.email.deliver_html_email", side_effect=RuntimeError("down")):
        assert services.staff_password_reset(account) is False


def test_password_reset_link_is_the_reset_token(outbound_on, account):
    services.send_password_reset(account.email)
    token = mail.outbox[0].body.split("/account/password/")[1].split("/")[1]
    assert customer_reset_token.check_token(account, token)
    assert customer_token.check_token(account, token) is False


def test_change_password_needs_the_current_one(account):
    with pytest.raises(services.CustomerError, match=services.WRONG_CURRENT) as refused:
        services.change_password(account, "wrong", PW)
    assert refused.value.field == "current_password"
    services.change_password(account, "correct horse battery", PW)
    account.refresh_from_db()
    assert account.check_password(PW)


def test_change_password_runs_the_password_validators(account):
    with pytest.raises(services.CustomerError, match="at least 10 characters") as refused:
        services.change_password(account, "correct horse battery", "short")
    assert refused.value.field == "new_password1"
    account.refresh_from_db()
    assert account.check_password("correct horse battery")
