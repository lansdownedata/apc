"""The staff side of the customer portal (spec 2026-11-20 §12): the Customer portal access
card on the contact profile, its four actions, and the chip on portal leads."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.core import outbound
from apps.core.choices import Channel
from apps.customers import services, staff_access
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount
from apps.leads.factories import LeadFactory
from apps.planners import services as planner_services
from apps.planners.models import PlannerAccount

pytestmark = pytest.mark.django_db

ACTIONS = ("customer_invite", "customer_resend", "customer_revoke", "customer_reset")
AFFILIATE = "Portal access is not available for affiliate companies."


def _profile(client: Client, contact) -> str:
    response = client.get(reverse("contact_detail", args=[contact.pk]))
    assert response.status_code == 200
    return response.content.decode()


def _card(html: str) -> str:
    """The Customer portal card's own markup, so a word elsewhere on the page can't pass."""
    start = html.index('aria-label="Customer portal"')
    return html[start : html.index("</section>", start)]


# --- the card ---------------------------------------------------------------------------


def test_card_states_none_invited_active_revoked(staff_client, outbound_on):
    tarrick = UserFactory(first_name="Tarrick", last_name="Jones")
    nov20 = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)  # 12:00 PM EST

    none = ContactFactory(name="Dana Whitfield")
    card = _card(_profile(staff_client, none))
    assert "Not invited" in card
    assert "Dana can sign in to request trips, see history and download receipts." in card
    assert "Send invite" in card
    assert reverse("customer_invite", args=[none.pk]) in card
    assert reverse("customer_revoke", args=[none.pk]) not in card
    assert 'aria-disabled="true"' not in card

    invited = CustomerAccountFactory(invited=True, invited_at=nov20, invited_by=tarrick)
    card = _card(_profile(staff_client, invited.contact))
    assert "Invited" in card
    assert "Invite emailed Nov 20, 2026 by Tarrick Jones." in card
    assert reverse("customer_resend", args=[invited.contact.pk]) in card
    assert reverse("customer_revoke", args=[invited.contact.pk]) in card
    assert reverse("customer_invite", args=[invited.contact.pk]) not in card

    active = CustomerAccountFactory(
        self_registered=True,
        registered_at=datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
        last_login=datetime(2026, 11, 20, 19, 14, tzinfo=UTC),
    )
    card = _card(_profile(staff_client, active.contact))
    assert "Active" in card
    assert "Signed up on their own Nov 12, 2026. Last signed in Nov 20 at 2:14 PM EST." in card
    assert "Send password reset" in card
    assert "Revoke access" in card
    assert reverse("customer_reset", args=[active.contact.pk]) in card
    assert reverse("customer_invite", args=[active.contact.pk]) not in card

    accepted = CustomerAccountFactory(
        invited_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
        invited_by=tarrick,
        registered_at=datetime(2026, 11, 12, 15, 0, tzinfo=UTC),
    )
    card = _card(_profile(staff_client, accepted.contact))
    assert "Invited Nov 10, 2026 by Tarrick Jones. Signed up Nov 12, 2026." in card

    revoked = CustomerAccountFactory(revoked_at=nov20, revoked_by=tarrick)
    card = _card(_profile(staff_client, revoked.contact))
    assert "Revoked" in card
    assert "Access revoked Nov 20, 2026 by Tarrick Jones." in card
    assert "Send invite" in card
    assert reverse("customer_invite", args=[revoked.contact.pk]) in card
    assert reverse("customer_revoke", args=[revoked.contact.pk]) not in card


def test_access_for_reports_status_and_what_applies(outbound_on):
    contact = ContactFactory()
    assert staff_access.access_for(contact)["status"] == "none"
    assert staff_access.access_for(contact)["can_invite"] is True
    assert staff_access.access_for(contact)["blocked_reason"] == ""
    account = CustomerAccountFactory(invited=True)
    access = staff_access.access_for(account.contact)
    assert (access["status"], access["can_invite"]) == ("invited", False)
    assert staff_access.access_for(CustomerAccountFactory().contact)["status"] == "active"
    revoked = staff_access.access_for(CustomerAccountFactory(revoked=True).contact)
    assert (revoked["status"], revoked["can_invite"]) == ("revoked", True)


def test_revoke_confirms_through_the_app_modal(staff_client, outbound_on):
    account = CustomerAccountFactory(invited=True)
    card = _card(_profile(staff_client, account.contact))
    assert "$store.modal.confirm" in card
    assert "variant: 'danger'" in card
    assert "Revoke portal access" in card


# --- invite -----------------------------------------------------------------------------


def test_invite_sends_mail_and_shows_invited(staff_client, outbound_on):
    contact = ContactFactory(name="Dana Whitfield", email="dana@example.com")
    url = reverse("customer_invite", args=[contact.pk])
    response = staff_client.post(url, follow=True)
    assert response.redirect_chain == [(reverse("contact_detail", args=[contact.pk]), 302)]
    (message,) = mail.outbox
    assert message.to == ["dana@example.com"]
    account = CustomerAccount.objects.get(contact=contact)
    assert account.status == CustomerAccount.Status.INVITED
    html = response.content.decode()
    assert "Invite sent to dana@example.com." in html
    card = _card(html)
    assert "Invite emailed" in card
    assert reverse("customer_resend", args=[contact.pk]) in card


def test_invite_blocked_without_contact_email_explains_why(staff_client, outbound_on):
    contact = ContactFactory(name="Dana Whitfield", email=None)
    card = _card(_profile(staff_client, contact))
    assert "Add an email address for Dana Whitfield before inviting them." in card
    assert 'aria-disabled="true"' in card
    response = staff_client.post(reverse("customer_invite", args=[contact.pk]), follow=True)
    assert response.status_code == 200
    assert "Add an email address for Dana Whitfield before inviting them." in (
        response.content.decode()
    )
    assert not CustomerAccount.objects.exists()
    assert mail.outbox == []


def test_invite_blocked_while_outbound_is_off_explains_why(staff_client, settings):
    settings.OUTBOUND_ENABLED = False
    contact = ContactFactory()
    card = _card(_profile(staff_client, contact))
    assert outbound.OFF_MESSAGE in card
    assert 'aria-disabled="true"' in card
    response = staff_client.post(reverse("customer_invite", args=[contact.pk]), follow=True)
    assert response.status_code == 200
    assert outbound.OFF_MESSAGE in response.content.decode()
    assert not CustomerAccount.objects.exists()


def test_invite_of_an_invited_contact_is_a_flash_not_an_error(staff_client, outbound_on):
    account = CustomerAccountFactory(invited=True)
    url = reverse("customer_invite", args=[account.contact.pk])
    response = staff_client.post(url, follow=True)
    assert response.status_code == 200
    assert "Use Resend to send a new link." in response.content.decode()
    assert mail.outbox == []


def test_a_revoked_contact_can_be_invited_again(staff_client, outbound_on):
    account = CustomerAccountFactory(revoked=True)
    staff_client.post(reverse("customer_invite", args=[account.contact.pk]))
    account.refresh_from_db()
    assert account.status == CustomerAccount.Status.INVITED
    assert len(mail.outbox) == 1


def test_affiliate_company_contact_is_blocked_and_refused(staff_client, outbound_on):
    contact = ContactFactory(company=CompanyFactory(is_affiliate_account=True))
    access = staff_access.access_for(contact)
    assert (access["can_invite"], access["blocked_reason"]) == (False, AFFILIATE)
    assert AFFILIATE in _card(_profile(staff_client, contact))
    with pytest.raises(services.CustomerError, match=AFFILIATE):
        services.invite(contact, actor=UserFactory())
    response = staff_client.post(reverse("customer_invite", args=[contact.pk]), follow=True)
    assert AFFILIATE in response.content.decode()
    assert not CustomerAccount.objects.exists()
    assert mail.outbox == []


def test_an_active_affiliate_account_stays_revocable(staff_client, outbound_on):
    account = CustomerAccountFactory(
        contact__company=CompanyFactory(is_affiliate_account=True),
    )
    assert reverse("customer_revoke", args=[account.contact.pk]) in _card(
        _profile(staff_client, account.contact)
    )
    staff_client.post(reverse("customer_revoke", args=[account.contact.pk]))
    account.refresh_from_db()
    assert account.status == CustomerAccount.Status.REVOKED


# --- resend, revoke, reset ----------------------------------------------------------------


def test_resend_sends_a_new_link(staff_client, outbound_on):
    account = CustomerAccountFactory(invited=True)
    response = staff_client.post(reverse("customer_resend", args=[account.contact.pk]), follow=True)
    assert f"Invite sent to {account.email}." in response.content.decode()
    assert len(mail.outbox) == 1


def test_resend_of_an_active_account_is_a_flash_not_an_error(staff_client, outbound_on):
    account = CustomerAccountFactory()
    response = staff_client.post(reverse("customer_resend", args=[account.contact.pk]), follow=True)
    assert response.status_code == 200
    assert "Only a pending invite can be resent." in response.content.decode()
    assert mail.outbox == []


def test_revoke_ends_customer_session(staff_client, client_as, outbound_on):
    agent = UserFactory(first_name="Tarrick", last_name="Jones")
    account = CustomerAccountFactory()
    customer = client_as(account)
    assert customer.get(reverse("customers:home")).status_code == 200
    staff = Client()
    staff.force_login(agent)
    response = staff.post(reverse("customer_revoke", args=[account.contact.pk]), follow=True)
    assert f"{account.contact.name} can no longer sign in to the customer portal." in (
        response.content.decode()
    )
    account.refresh_from_db()
    assert account.status == CustomerAccount.Status.REVOKED
    assert account.revoked_by == agent
    response = customer.get(reverse("customers:home"))
    assert response.status_code == 302
    assert response.url.startswith(reverse("customers:login"))


def test_reset_sends_password_mail(staff_client, outbound_on):
    account = CustomerAccountFactory()
    response = staff_client.post(reverse("customer_reset", args=[account.contact.pk]), follow=True)
    assert f"Password reset sent to {account.email}." in response.content.decode()
    (message,) = mail.outbox
    assert message.to == [account.email]
    assert "/account/password/" in message.body


def test_reset_past_the_hourly_limit_does_not_claim_a_send(staff_client, outbound_on):
    account = CustomerAccountFactory()
    for _ in range(services.MAILS_PER_EMAIL):
        assert services.send_password_reset(account.email) is True
    response = staff_client.post(reverse("customer_reset", args=[account.contact.pk]), follow=True)
    html = response.content.decode()
    assert "Password reset sent" not in html
    assert "The reset email was not sent." in html
    assert len(mail.outbox) == services.MAILS_PER_EMAIL


def test_reset_while_outbound_is_off_is_refused(staff_client, settings):
    settings.OUTBOUND_ENABLED = False
    account = CustomerAccountFactory()
    response = staff_client.post(reverse("customer_reset", args=[account.contact.pk]), follow=True)
    assert outbound.OFF_MESSAGE in response.content.decode()
    assert mail.outbox == []


def test_reset_of_an_invited_account_is_a_flash_not_an_error(staff_client, outbound_on):
    account = CustomerAccountFactory(invited=True)
    response = staff_client.post(reverse("customer_reset", args=[account.contact.pk]), follow=True)
    assert response.status_code == 200
    assert "Only a customer who has signed up can get a password reset." in (
        response.content.decode()
    )
    assert mail.outbox == []


def test_reset_for_a_contact_whose_email_was_cleared_is_blocked(staff_client, outbound_on):
    account = CustomerAccountFactory(contact__name="Dana Whitfield")
    account.contact.email = None
    account.contact.save(update_fields=["email"])
    card = _card(_profile(staff_client, account.contact))
    assert "Add an email address for Dana Whitfield first." in card
    assert 'aria-disabled="true"' in card
    response = staff_client.post(reverse("customer_reset", args=[account.contact.pk]), follow=True)
    assert "Add an email address for Dana Whitfield first." in response.content.decode()
    assert mail.outbox == []


# --- who may act ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ACTIONS)
def test_anonymous_and_customer_sessions_cannot_reach_staff_actions(
    client, client_as, outbound_on, name
):
    account = CustomerAccountFactory(invited=True)
    before = CustomerAccount.objects.values().get(pk=account.pk)
    for visitor in (client, client_as(CustomerAccountFactory())):
        response = visitor.post(reverse(name, args=[account.contact.pk]))
        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
    assert CustomerAccount.objects.values().get(pk=account.pk) == before
    assert mail.outbox == []


@pytest.mark.parametrize("name", ACTIONS)
def test_staff_actions_are_post_only_and_404_for_an_unknown_contact(staff_client, name):
    account = CustomerAccountFactory(invited=True)
    assert staff_client.get(reverse(name, args=[account.contact.pk])).status_code == 405
    assert staff_client.post(reverse(name, args=[account.contact.pk + 999])).status_code == 404


def test_staff_actions_need_the_csrf_token(outbound_on):
    contact = ContactFactory()
    client = Client(enforce_csrf_checks=True)
    client.force_login(UserFactory())
    assert client.post(reverse("customer_invite", args=[contact.pk])).status_code == 403
    assert not CustomerAccount.objects.exists()


def test_actions_touch_only_the_contact_in_the_url(staff_client, outbound_on):
    mine = CustomerAccountFactory(invited=True)
    other = CustomerAccountFactory(invited=True)
    staff_client.post(reverse("customer_revoke", args=[mine.contact.pk]))
    other.refresh_from_db()
    assert other.status == CustomerAccount.Status.INVITED


# --- alongside planner access, the chip, the query count --------------------------------


def test_contact_can_hold_planner_and_customer_access(staff_client, outbound_on):
    contact = ContactFactory(email="dana@example.com", phone="(571) 555-0142")
    planner_services.invite(contact, actor=UserFactory())
    staff_client.post(reverse("customer_invite", args=[contact.pk]))
    assert PlannerAccount.objects.filter(contact=contact).exists()
    assert CustomerAccount.objects.filter(contact=contact).exists()
    html = _profile(staff_client, contact)
    assert "Planner access" in html
    assert reverse("planner_resend", args=[contact.pk]) in html
    assert reverse("customer_resend", args=[contact.pk]) in _card(html)


def test_pipeline_card_shows_portal_chip(staff_client):
    LeadFactory(channel=Channel.PORTAL, contact__name="Dana Whitfield")
    LeadFactory(channel=Channel.WEBSITE, contact__name="Sam Website")
    for url in (reverse("pipeline"), reverse("lead_list")):
        html = staff_client.get(url).content.decode()
        assert html.count('data-chip="portal"') == 1, url
        chip = html[html.index('data-chip="portal"') :]
        assert chip[: chip.index("</span>")].endswith("Customer portal")


def test_contact_detail_query_count_unchanged(
    staff_client, outbound_on, django_assert_max_num_queries
):
    """24 queries before this card; the account rides along on the contact's own query."""
    agent = UserFactory()
    account = CustomerAccountFactory(
        invited_at=timezone.now() - timedelta(days=3),
        invited_by=agent,
        revoked_at=timezone.now(),
        revoked_by=agent,
    )
    url = reverse("contact_detail", args=[account.contact.pk])
    staff_client.get(url)
    with django_assert_max_num_queries(24):
        assert staff_client.get(url).status_code == 200
    plain = ContactFactory()
    with django_assert_max_num_queries(24):
        assert staff_client.get(reverse("contact_detail", args=[plain.pk])).status_code == 200
