"""APC-66: planner accounts — invite, register, sign in, revoke, and which weddings they see."""

from unittest.mock import patch

import pytest

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.core import outbound
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.planners import services
from apps.planners.models import PlannerAccount
from apps.planners.tokens import planner_token

pytestmark = pytest.mark.django_db

SEND = "apps.planners.services.send_html_email"


@pytest.fixture
def agent():
    return UserFactory()


@pytest.fixture
def planner():
    return ContactFactory(name="Dana Planner", email="Dana@Example.com")


def _invite(contact, agent):
    with patch(SEND, return_value=True) as send:
        account, sent = services.invite(contact, actor=agent)
    return account, sent, send


def test_invite_creates_a_pending_account_and_emails_a_registration_link(planner, agent):
    account, sent, send = _invite(planner, agent)
    assert sent is True
    assert account.contact == planner
    assert account.email == "dana@example.com"
    assert account.status == PlannerAccount.Status.INVITED
    assert account.invited_by == agent
    assert account.has_usable_password() is False
    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["to"] == "dana@example.com"
    assert kwargs["template"] == "planner_invite"
    assert "/planner/register/" in kwargs["context"]["register_url"]
    # A planner is a customer: the go-live switch applies.
    assert kwargs.get("internal", False) is False


def test_invite_is_refused_while_outbound_is_off_and_nothing_changes(planner, agent, settings):
    settings.OUTBOUND_ENABLED = False
    with patch(SEND) as send, pytest.raises(services.PlannerError) as exc:
        services.invite(planner, actor=agent)
    assert str(exc.value) == outbound.OFF_MESSAGE
    send.assert_not_called()
    assert not PlannerAccount.objects.exists()


def test_invite_needs_an_email(agent):
    contact = ContactFactory(email=None)
    with pytest.raises(services.PlannerError, match="email"):
        services.invite(contact, actor=agent)


def test_invite_refuses_someone_who_already_has_access(planner, agent):
    account, _, _ = _invite(planner, agent)
    services.register(account, "a-long-pass-phrase-77")
    with pytest.raises(services.PlannerError, match="already"):
        services.invite(planner, actor=agent)


def test_register_sets_the_password_and_stamps_registration(planner, agent):
    account, _, _ = _invite(planner, agent)
    services.register(account, "a-long-pass-phrase-77")
    account.refresh_from_db()
    assert account.status == PlannerAccount.Status.REGISTERED
    assert account.registered_at is not None
    assert account.check_password("a-long-pass-phrase-77")


def test_the_registration_link_dies_once_used(planner, agent):
    account, _, _ = _invite(planner, agent)
    token = planner_token.make_token(account)
    services.register(account, "a-long-pass-phrase-77")
    account.refresh_from_db()
    assert planner_token.check_token(account, token) is False


def test_resend_kills_the_previous_link(planner, agent):
    account, _, _ = _invite(planner, agent)
    old = planner_token.make_token(account)
    with patch(SEND, return_value=True):
        services.resend(account)
    account.refresh_from_db()
    assert planner_token.check_token(account, old) is False
    assert planner_token.check_token(account, planner_token.make_token(account))


def test_resend_is_refused_while_outbound_is_off(planner, agent, settings):
    account, _, _ = _invite(planner, agent)
    settings.OUTBOUND_ENABLED = False
    with patch(SEND) as send, pytest.raises(services.PlannerError):
        services.resend(account)
    send.assert_not_called()


def test_authenticate_accepts_the_registered_password_only(planner, agent):
    account, _, _ = _invite(planner, agent)
    assert services.authenticate("dana@example.com", "anything") is None  # not registered
    services.register(account, "a-long-pass-phrase-77")
    assert services.authenticate("DANA@example.com", "a-long-pass-phrase-77") == account
    assert services.authenticate("dana@example.com", "wrong") is None
    assert services.authenticate("nobody@example.com", "a-long-pass-phrase-77") is None


def test_a_revoked_account_cannot_sign_in_and_reinviting_restores_it(planner, agent):
    account, _, _ = _invite(planner, agent)
    services.register(account, "a-long-pass-phrase-77")
    link = planner_token.make_token(account)
    services.revoke(account, actor=agent)
    account.refresh_from_db()
    assert account.status == PlannerAccount.Status.REVOKED
    assert services.authenticate("dana@example.com", "a-long-pass-phrase-77") is None
    assert planner_token.check_token(account, link) is False

    account, sent, _ = _invite(planner, agent)
    assert sent is True
    assert account.status == PlannerAccount.Status.INVITED
    services.register(account, "another-long-phrase-88")
    assert services.authenticate("dana@example.com", "another-long-phrase-88") == account


# --- which weddings ---------------------------------------------------------------------


def test_a_planner_sees_only_weddings_they_hold_a_role_on(planner, agent):
    account, _, _ = _invite(planner, agent)
    mine = LeadFactory(status=Lead.Status.BOOKED)
    LeadFactory(status=Lead.Status.BOOKED)  # someone else's
    assert list(services.weddings_for(account)) == []

    role = LeadContact.objects.create(
        lead=mine, contact=planner, role=LeadContact.Role.LEAD_PLANNER
    )
    assert list(services.weddings_for(account)) == [mine]

    role.delete()
    assert list(services.weddings_for(account)) == []


def test_two_roles_on_one_wedding_list_it_once(planner, agent):
    account, _, _ = _invite(planner, agent)
    lead = LeadFactory(status=Lead.Status.BOOKED)
    for role in (LeadContact.Role.LEAD_PLANNER, LeadContact.Role.DAY_OF_COORDINATOR):
        LeadContact.objects.create(lead=lead, contact=planner, role=role)
    assert list(services.weddings_for(account)) == [lead]


def test_a_lost_order_is_not_a_wedding_the_planner_sees(planner, agent):
    account, _, _ = _invite(planner, agent)
    lead = LeadFactory(status=Lead.Status.LOST)
    LeadContact.objects.create(lead=lead, contact=planner, role=LeadContact.Role.LEAD_PLANNER)
    assert list(services.weddings_for(account)) == []
